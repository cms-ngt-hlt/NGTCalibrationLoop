"""Tests for airflow_automation/airflow_dags/ngt_dags_per_file.py -- the run-detector /
file-detector (deferred) / per-file-processing design: DAG-wiring integrity plus
direct unit tests of each task's python_callable, with airflow_api and
job-launching monkeypatched out -- no live scheduler/triggerer/DB/API server
needed. NewFileTrigger's own async run() is exercised directly too, driven
to its first yielded event with a single asyncio.run() call (no pytest-
asyncio dependency needed for that).

Skipped everywhere apache-airflow isn't installed (importorskip below). Where it
is, source airflow_automation/airflow_demo/airflow_env.sh before running pytest so
that importing `airflow` picks up the AIRFLOW_HOME/executor/DB settings the DAGs
were built and verified against (see README.md's Airflow setup section).
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("airflow")

import omsapi  # noqa: E402  (test stub, see tests/stubs/omsapi/__init__.py)
from airflow.models import DagBag  # noqa: E402
from airflow.sdk.exceptions import AirflowSkipException  # noqa: E402

AIRFLOW_DAGS_DIR = Path(__file__).resolve().parents[2] / "airflow_automation" / "airflow_dags"
if str(AIRFLOW_DAGS_DIR.parent) not in sys.path:  # so `import airflow_dags` resolves
    sys.path.insert(0, str(AIRFLOW_DAGS_DIR.parent))

import airflow_dags.ngt_dags_per_file as pf  # noqa: E402
from airflow_dags.triggers import NewFileTrigger  # noqa: E402
from ngt_calibration_loop import step2 as step2_lib  # noqa: E402

CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")


def make_run(run_number, start_time, end_time=None, last_ls=100):
    return {
        "run_number": run_number,
        "fill_type_runtime": "PROTONS",
        "l1_hlt_mode": "collisions2026",
        "stable_beam": True,
        "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ") if end_time else None,
        "last_lumisection_number": last_ls,
    }


class FakeTaskInstance:
    def __init__(self):
        self._store = {}

    def xcom_push(self, key, value):
        self._store[key] = value

    def xcom_pull(self, task_ids=None, key=None):
        return self._store.get(key)


def _context(conf):
    return {"dag_run": SimpleNamespace(conf=conf), "ti": FakeTaskInstance()}


def _run_first_event(trigger):
    """Drive an async-generator Trigger.run() to its first yielded event,
    without needing pytest-asyncio -- one asyncio.run() around a small local
    coroutine that pulls exactly one item."""

    async def _first():
        async for event in trigger.run():
            return event.payload
        return None

    return asyncio.run(_first())


# --- DAG wiring integrity ------------------------------------------------------------


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder=str(AIRFLOW_DAGS_DIR))


def test_dagbag_has_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_all_dags_present(dagbag):
    expected = {pf.RUN_DETECTOR_DAG_ID, pf.FILE_DETECTOR_DAG_ID}
    expected |= {pf._process_dag_id(c) for c in CALIBRATIONS}
    assert expected <= set(dagbag.dags)


def test_process_dag_exists_separately_per_calibration(dagbag):
    # The actual ask this covers: one process DAG per calibration (built from
    # one shared factory, _build_process_dag), not one generic DAG shared by
    # conf -- so each is independently visible/pausable/graphable in the UI.
    dag_ids = {pf._process_dag_id(c) for c in CALIBRATIONS}
    assert dag_ids == {"ngt_perfile_process_sistripbad", "ngt_perfile_process_ecalpedestals", "ngt_perfile_process_beamspot"}
    assert dag_ids <= set(dagbag.dags)
    for calibration in CALIBRATIONS:
        dag = dagbag.dags[pf._process_dag_id(calibration)]
        assert calibration in dag.tags


def test_run_detector_has_one_task_per_calibration(dagbag):
    dag = dagbag.dags[pf.RUN_DETECTOR_DAG_ID]
    task_ids = {t.task_id for t in dag.topological_sort()}
    assert task_ids == {f"detect_{c.lower()}" for c in CALIBRATIONS}
    assert dag.timetable.delta == timedelta(seconds=30)
    assert dag.catchup is False


def test_file_detector_task_structure(dagbag):
    dag = dagbag.dags[pf.FILE_DETECTOR_DAG_ID]
    assert [t.task_id for t in dag.topological_sort()] == ["wait_for_files", "dispatch_and_continue"]


def test_process_dag_task_structure_and_ordering(dagbag):
    dag = dagbag.dags[pf._process_dag_id("EcalPedestals")]
    assert [t.task_id for t in dag.topological_sort()] == [
        "step2_express", "step3_alca", "step4_harvest", "step4_upload",
    ]


def test_process_dag_launch_tasks_have_retries_and_timeout(dagbag):
    dag = dagbag.dags[pf._process_dag_id("EcalPedestals")]
    for task_id in ("step2_express", "step3_alca", "step4_harvest"):
        task = dag.get_task(task_id)
        assert task.retries == 3
        assert task.retry_exponential_backoff is True
        assert task.execution_timeout == timedelta(hours=2)


def test_process_dag_upload_has_independent_retry_policy(dagbag):
    dag = dagbag.dags[pf._process_dag_id("EcalPedestals")]
    upload = dag.get_task("step4_upload")
    harvest = dag.get_task("step4_harvest")
    assert upload.retries == 5
    assert upload.retries != harvest.retries  # decoupled from the harvest


# --- run detector callable -------------------------------------------------------------


def test_run_detector_dispatches_file_detector_when_run_found(isolated_env, monkeypatch):
    triggered = []
    monkeypatch.setattr(
        pf.airflow_api, "trigger_dag_run",
        lambda dag_id, run_id, conf: triggered.append((dag_id, run_id, conf)) or {"dag_run_id": run_id},
    )
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])

    pf._make_run_detector_callable("EcalPedestals")()

    assert len(triggered) == 1
    dag_id, run_id, conf = triggered[0]
    assert dag_id == pf.FILE_DETECTOR_DAG_ID
    assert run_id == "ecalpedestals_run398601__cycle0"
    assert conf == {"calibration": "EcalPedestals", "run_number": "398601", "cycle": 0}


def test_run_detector_skips_when_nothing_found(isolated_env):
    omsapi.configure_runs([])
    with pytest.raises(AirflowSkipException):
        pf._make_run_detector_callable("EcalPedestals")()


def test_run_detector_tolerates_already_dispatched(isolated_env, monkeypatch):
    """step2.find_new_run's own run_dir dedup should normally prevent this,
    but airflow_api.trigger_dag_run returning None (409-swallowed) must not
    raise either -- belt and suspenders, see trigger_dag_run's docstring."""
    monkeypatch.setattr(pf.airflow_api, "trigger_dag_run", lambda *a, **k: None)
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])

    pf._make_run_detector_callable("EcalPedestals")()  # must not raise


# --- file detector: NewFileTrigger ------------------------------------------------------


def test_new_file_trigger_yields_batch_event_when_file_available(isolated_env, fake_eos):
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)
    fake_eos.add_file(f"{ctx.path_where_files_appear}/run398601_ls0051.root", run_number=398601, ls_numbers=[51])

    trigger = NewFileTrigger("EcalPedestals", run_number, poll_interval=0.01)
    event = _run_first_event(trigger)

    assert event["action"] == "batch"
    assert len(event["files"]) == 1


def test_new_file_trigger_waits_then_yields_once_file_appears(isolated_env, fake_eos, monkeypatch):
    """Polls a couple of times (action='wait') before the file shows up --
    proves the trigger actually loops rather than only working when the file
    is already there on the first check."""
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)

    calls = {"n": 0}
    real_check = step2_lib.check_ls_for_processing

    def _flaky_check(c):
        calls["n"] += 1
        if calls["n"] < 3:
            return step2_lib.CycleDecision(action="wait", ls_to_process=set(), still_have_time=True)
        return real_check(c)

    monkeypatch.setattr(step2_lib, "check_ls_for_processing", _flaky_check)
    fake_eos.add_file(f"{ctx.path_where_files_appear}/run398601_ls0051.root", run_number=398601, ls_numbers=[51])

    trigger = NewFileTrigger("EcalPedestals", run_number, poll_interval=0.01)
    event = _run_first_event(trigger)

    assert calls["n"] == 3
    assert event["action"] == "batch"


def test_new_file_trigger_serializes_roundtrippable_args():
    trigger = NewFileTrigger("EcalPedestals", 398601, poll_interval=7.5, run_end_grace_seconds=45)
    classpath, kwargs = trigger.serialize()
    assert classpath == "airflow_dags.triggers.NewFileTrigger"
    assert kwargs == {
        "calibration": "EcalPedestals", "run_number": 398601, "poll_interval": 7.5, "run_end_grace_seconds": 45,
    }


# --- file detector: NewFileTrigger's run-end grace period (independent of ---
# step2.check_ls_for_processing's own run-start-relative max_latch_time_hours;
# see triggers.NewFileTrigger's docstring for why this logic lives here) ----


def test_run_ended_grace_expired_false_while_still_running():
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])
    trigger = NewFileTrigger("EcalPedestals", 398601, run_end_grace_seconds=1)
    assert trigger._run_ended_grace_expired() is False


def test_run_ended_grace_expired_false_within_grace_period():
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398601, start_time=now - timedelta(hours=1), end_time=now - timedelta(seconds=2))])
    trigger = NewFileTrigger("EcalPedestals", 398601, run_end_grace_seconds=30)
    assert trigger._run_ended_grace_expired() is False


def test_run_ended_grace_expired_true_past_grace_period():
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398601, start_time=now - timedelta(hours=1), end_time=now - timedelta(seconds=2))])
    trigger = NewFileTrigger("EcalPedestals", 398601, run_end_grace_seconds=1)
    assert trigger._run_ended_grace_expired() is True


def test_new_file_trigger_yields_final_via_run_end_grace_not_check_ls_own_timeout(isolated_env, fake_eos):
    """Regression test for the reported gap: a run started only 1h ago (so
    step2.check_ls_for_processing's OWN give-up branch -- max_latch_time_hours,
    8h default -- would NOT fire) but ended 2s ago with zero matching files
    ever seeded. The trigger's own run_end_grace_seconds must still finalize
    it, independent of step2.py's run-start-relative timeout, rather than
    leaving wait_for_files deferred for the rest of that 8h budget."""
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398601, start_time=now - timedelta(hours=1), end_time=now - timedelta(seconds=2), last_ls=100)]
    )
    run_number = step2_lib.find_new_run("EcalPedestals")
    assert run_number == 398601
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)
    # Confirm check_ls_for_processing's own view genuinely would NOT finalize
    # this on its own (still has run-start-relative time left, and files never
    # reconciled) -- isolates that the grace period is what's actually doing it.
    assert step2_lib.check_ls_for_processing(ctx).action == "wait"

    trigger = NewFileTrigger("EcalPedestals", run_number, poll_interval=0.01, run_end_grace_seconds=1)
    event = _run_first_event(trigger)

    assert event == {"action": "final", "files": []}


# --- file detector: dispatch_and_continue -----------------------------------------------


def test_dispatch_and_continue_dispatches_each_file_and_retriggers_when_not_final(isolated_env, monkeypatch):
    dispatched = []
    monkeypatch.setattr(
        pf.airflow_api, "trigger_dag_run",
        lambda dag_id, run_id, conf: dispatched.append((dag_id, run_id, conf)) or {"dag_run_id": run_id},
    )
    context = _context({"calibration": "EcalPedestals", "run_number": "398601", "cycle": 0})
    # dispatch_and_continue pulls wait_for_files' XCom with no explicit key --
    # in real Airflow that means "return_value" (what execute_complete
    # returned); FakeTaskInstance simplifies that to its own key=None default,
    # so push under that same key here.
    context["ti"].xcom_push(key=None, value={"action": "batch", "files": ["/tmp/a.root", "/tmp/b.root"]})

    pf._make_dispatch_and_continue_callable()(**context)

    process_dag_id = pf._process_dag_id("EcalPedestals")
    process_dispatches = [d for d in dispatched if d[0] == process_dag_id]
    file_detector_dispatches = [d for d in dispatched if d[0] == pf.FILE_DETECTOR_DAG_ID]
    assert len(process_dispatches) == 2
    assert {d[2]["file"] for d in process_dispatches} == {"/tmp/a.root", "/tmp/b.root"}
    # No "calibration" in conf -- it's implied by which per-calibration DAG
    # got triggered (process_dag_id above), unlike file_detector's conf.
    assert all("calibration" not in d[2] and d[2]["run_number"] == "398601" for d in process_dispatches)
    assert len(file_detector_dispatches) == 1  # re-armed for the next cycle
    assert file_detector_dispatches[0][2]["cycle"] == 1


def test_dispatch_and_continue_final_cycle_writes_run_end_log_and_stops(isolated_env, monkeypatch):
    dispatched = []
    monkeypatch.setattr(
        pf.airflow_api, "trigger_dag_run",
        lambda dag_id, run_id, conf: dispatched.append((dag_id, run_id, conf)) or {"dag_run_id": run_id},
    )
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)

    context = _context({"calibration": "EcalPedestals", "run_number": str(run_number), "cycle": 3})
    context["ti"].xcom_push(key=None, value={"action": "final", "files": []})

    pf._make_dispatch_and_continue_callable()(**context)

    assert (ctx.working_dir / step2_lib.RUN_END_LOG_NAME).exists()
    assert not any(d[0] == pf.FILE_DETECTOR_DAG_ID for d in dispatched)  # not re-armed


def test_job_run_id_for_file_is_deterministic():
    a = pf._job_run_id_for_file("398601", "/data/ngt/EcalPedestals/run398601/x.root")
    b = pf._job_run_id_for_file("398601", "/data/ngt/EcalPedestals/run398601/x.root")
    c = pf._job_run_id_for_file("398601", "/data/ngt/EcalPedestals/run398601/y.root")
    assert a == b
    assert a != c


def test_job_run_id_for_file_embeds_the_lumisection_number():
    # scenario-player/seed.py's real raw-file naming convention (_touch_ls_file). No
    # calibration prefix -- that's implied by which per-calibration DAG
    # (_process_dag_id) this run_id belongs to, unlike file_detector's.
    run_id = pf._job_run_id_for_file("398601", "/eos/EcalPedestals/398/601/00000/run398601_ls0051.root")
    assert run_id.startswith("run398601__ls0051__")


def test_job_run_id_for_file_falls_back_cleanly_when_ls_not_parseable():
    # A filename that doesn't match scenario-player/seed.py's convention (e.g. a real,
    # differently-named EOS file) must still produce a valid, unique id --
    # just without a readable ls-number segment.
    run_id = pf._job_run_id_for_file("398601", "/data/ngt/EcalPedestals/run398601/x.root")
    assert run_id.startswith("run398601__")
    assert "__ls" not in run_id


# --- processing DAG task callables, chained through one file ---------------------------


def test_process_dag_runs_one_file_through_all_three_steps(isolated_env, fake_eos, job_runner):
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398601, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx2 = step2_lib.build_run_context("EcalPedestals", run_number)
    input_file = f"{ctx2.path_where_files_appear}/run398601_ls0051.root"
    fake_eos.add_file(input_file, run_number=398601, ls_numbers=[51])

    context = _context({"run_number": str(run_number), "file": input_file})

    pf._make_step2_express_callable("EcalPedestals")(**context)
    step2_output = context["ti"].xcom_pull(key="step2_output")
    assert step2_output is not None
    assert len(job_runner) == 1
    # job_runner records the launch but never actually runs the script (see
    # its docstring in conftest.py) -- manufacture the file it would have
    # produced, same convention fake_eos.add_file already follows above.
    Path(step2_output).write_bytes(b"(FAKE) RECO output\n")

    pf._make_step3_alca_callable("EcalPedestals")(**context)
    assert len(job_runner) == 2
    # Same manufacturing as above, one step further: the (mocked) ALCA job's
    # witness file + ALCARECO output, using EcalPedestals.yaml's real
    # step_3_witness_suffix/step_3_root_filename.
    alca_job_dir = next(p for p in ctx2.working_dir.iterdir() if p.name.startswith("alcaPromptJob_"))
    (alca_job_dir / "ecalPedsStep3_job.txt").touch()
    (alca_job_dir / "PromptCalibProdEcalPedestals.root").write_bytes(b"(FAKE) ALCARECO output\n")

    pf._make_step4_harvest_callable("EcalPedestals")(**context)
    assert len(job_runner) == 3

    pf._make_step4_upload_callable("EcalPedestals")(**context)
    assert len(job_runner) == 4
    assert job_runner[-1]["script_name"] == "UPLOAD.sh"

    # Bookkeeping matches the on-disk contract (README's directory table).
    run_dir = ctx2.working_dir
    assert (run_dir / step2_lib.PROCESSED_LOG_NAME).exists()
    from ngt_calibration_loop import step3 as step3_lib

    assert (run_dir / step3_lib.PROCESSED_LOG_NAME).exists()


def test_step4_harvest_skips_when_nothing_new(isolated_env):
    run_dir = isolated_env.data_dir / "EcalPedestals" / "run398601"
    run_dir.mkdir(parents=True)
    context = _context({"run_number": "398601", "file": "irrelevant"})
    with pytest.raises(AirflowSkipException):
        pf._make_step4_harvest_callable("EcalPedestals")(**context)
