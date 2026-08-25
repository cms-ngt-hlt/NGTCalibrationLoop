"""Tests for airflow_dags/ngt_dags.py: DAG-wiring integrity plus direct unit
tests of each task's python_callable (no live Airflow scheduler/webserver,
and no DagRun/XCom rows written to the real metadata DB -- trigger_dag calls
are monkeypatched out). Full scheduler-driven, end-to-end behavior is instead
exercised by dev/airflow_demo.sh's live demo -- see README.md.

Skipped everywhere apache-airflow isn't installed (e.g. the Windows-side
venv), via the importorskip below, so the rest of the suite stays runnable
without it. Where it *is* installed (the WSL venv), Airflow must already be
configured -- `source dev/airflow_env.sh` before running pytest -- so that
importing `airflow` picks up the AIRFLOW_HOME/executor/DB settings the DAGs
were built and verified against (see README.md's Airflow setup section).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("airflow")

import omsapi  # noqa: E402  (test stub, see tests/stubs/omsapi/__init__.py)
from airflow.exceptions import AirflowSkipException  # noqa: E402
from airflow.models import DagBag  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
AIRFLOW_DAGS_DIR = REPO_ROOT / "airflow_dags"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import airflow_dags.ngt_dags as ngt_dags  # noqa: E402
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
    """Minimal XCom double: enough for callables that push/pull by key within
    what would be a single DAG run -- task_ids is accepted but ignored since
    each key is only ever pushed by one task in these tests."""

    def __init__(self):
        self._store = {}

    def xcom_push(self, key, value):
        self._store[key] = value

    def xcom_pull(self, task_ids=None, key=None):
        return self._store.get(key)


def _context(conf):
    return {"dag_run": SimpleNamespace(conf=conf), "ti": FakeTaskInstance()}


# --- DAG wiring integrity ------------------------------------------------------------


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder=str(AIRFLOW_DAGS_DIR), include_examples=False)


def test_dagbag_has_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_all_expected_dags_present(dagbag):
    expected = set()
    for calibration in CALIBRATIONS:
        for step_num in (2, 3, 4):
            expected.add(ngt_dags._latch_dag_id(step_num, calibration))
            expected.add(ngt_dags._process_dag_id(step_num, calibration))
    assert expected == set(dagbag.dags)


def test_process_dag_task_structure(dagbag):
    step2_dag = dagbag.dags["ngt_step2_ecalpedestals_process"]
    assert [t.task_id for t in step2_dag.topological_sort()] == [
        "check_and_batch", "prepare_job", "launch_job", "finalize_cycle", "advance",
    ]
    step4_dag = dagbag.dags["ngt_step4_ecalpedestals_process"]
    assert [t.task_id for t in step4_dag.topological_sort()] == [
        "check_and_batch", "prepare_job", "launch_job", "upload_conditions", "finalize_cycle", "advance",
    ]


def test_latch_dags_run_frequently_with_no_backfill(dagbag):
    for calibration in CALIBRATIONS:
        dag = dagbag.dags[ngt_dags._latch_dag_id(2, calibration)]
        assert dag.schedule_interval == timedelta(seconds=30)
        assert dag.catchup is False
        assert dag.max_active_runs == 1


def test_process_dags_are_externally_triggered_only(dagbag):
    for calibration in CALIBRATIONS:
        for step_num in (2, 3, 4):
            dag = dagbag.dags[ngt_dags._process_dag_id(step_num, calibration)]
            assert dag.schedule_interval is None


def test_launch_job_has_retries_and_timeout(dagbag):
    launch_job = dagbag.dags["ngt_step2_ecalpedestals_process"].get_task("launch_job")
    assert launch_job.retries == 3
    assert launch_job.retry_exponential_backoff is True
    assert launch_job.execution_timeout == timedelta(hours=2)


def test_upload_conditions_has_independent_retry_policy(dagbag):
    dag = dagbag.dags["ngt_step4_ecalpedestals_process"]
    upload = dag.get_task("upload_conditions")
    launch = dag.get_task("launch_job")
    assert upload.retries == 5
    assert upload.retries != launch.retries  # decoupled from the harvesting retry policy


# --- Latch task callable ---------------------------------------------------------------


def test_latch_callable_triggers_processor_when_run_found(isolated_env, monkeypatch):
    triggered = []
    monkeypatch.setattr(
        ngt_dags, "_trigger_process_dag",
        lambda step_num, calibration, run_number, cycle: triggered.append((step_num, calibration, run_number, cycle)),
    )
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398600, start_time=start_time, end_time=None)])

    ngt_dags._make_latch_callable(2, "EcalPedestals")()

    assert triggered == [(2, "EcalPedestals", 398600, 0)]


def test_latch_callable_skips_when_nothing_found(isolated_env):
    omsapi.configure_runs([])
    with pytest.raises(AirflowSkipException):
        ngt_dags._make_latch_callable(2, "EcalPedestals")()


def test_latch_callable_step3_excludes_already_latched_runs(isolated_env, monkeypatch):
    (isolated_env.data_dir / "EcalPedestals" / "run398600").mkdir(parents=True)
    monkeypatch.setattr(ngt_dags, "_existing_run_numbers", lambda process_dag_id: {"398600"})
    triggered = []
    monkeypatch.setattr(ngt_dags, "_trigger_process_dag", lambda *a, **k: triggered.append((a, k)))

    with pytest.raises(AirflowSkipException):
        ngt_dags._make_latch_callable(3, "EcalPedestals")()
    assert triggered == []


# --- Process DAG task callables, chained through one cycle ----------------------------


def test_process_dag_full_cycle_batches_and_retriggers_self(isolated_env, fake_eos, job_runner, monkeypatch):
    monkeypatch.setattr(ngt_dags, "CYCLE_SLEEP_SECONDS", 0)
    triggered = []
    monkeypatch.setattr(
        ngt_dags, "_trigger_process_dag",
        lambda step_num, calibration, run_number, cycle: triggered.append((step_num, calibration, str(run_number), cycle)),
    )

    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398600, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)
    fake_eos.add_file(f"{ctx.path_where_files_appear}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    context = _context({"run_number": run_number, "cycle": 0})

    ngt_dags._make_check_and_batch_callable(2, "EcalPedestals")(**context)
    assert context["ti"].xcom_pull(key="action") == "batch"

    ngt_dags._make_prepare_job_callable(2, "EcalPedestals")(**context)  # must not raise/skip

    ngt_dags._make_launch_job_callable(2, "EcalPedestals")(**context)
    assert len(job_runner) == 1

    ngt_dags._make_finalize_callable(2, "EcalPedestals")(**context)
    assert context["ti"].xcom_pull(key="is_final") is False

    ngt_dags._make_advance_callable(2, "EcalPedestals")(**context)
    assert triggered == [(2, "EcalPedestals", str(run_number), 1)]  # same step, next cycle


def test_process_dag_final_cycle_hands_off_to_next_step(isolated_env, fake_eos, monkeypatch):
    monkeypatch.setattr(ngt_dags, "CYCLE_SLEEP_SECONDS", 0)
    triggered = []
    monkeypatch.setattr(
        ngt_dags, "_trigger_process_dag",
        lambda step_num, calibration, run_number, cycle: triggered.append((step_num, calibration, str(run_number), cycle)),
    )

    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(398600, start_time=start_time, end_time=None)])
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)
    # Age the persisted runStart.log past the 8h latch window -- _build_ctx re-reads it
    # from disk on every cycle, so mutating an in-memory ctx wouldn't be visible to it.
    old_start = datetime.now(timezone.utc) - timedelta(hours=9)
    (ctx.working_dir / step2_lib.RUN_START_LOG_NAME).write_text(old_start.isoformat(), encoding="utf-8")
    context = _context({"run_number": run_number, "cycle": 0})

    ngt_dags._make_check_and_batch_callable(2, "EcalPedestals")(**context)
    assert context["ti"].xcom_pull(key="action") == "final"

    with pytest.raises(AirflowSkipException):  # nothing to launch, no LS files ever arrived
        ngt_dags._make_prepare_job_callable(2, "EcalPedestals")(**context)

    ngt_dags._make_finalize_callable(2, "EcalPedestals")(**context)
    assert context["ti"].xcom_pull(key="is_final") is True

    ngt_dags._make_advance_callable(2, "EcalPedestals")(**context)
    assert triggered == [(3, "EcalPedestals", str(run_number), 0)]  # hands off to Step 3


def test_advance_from_step4_does_not_trigger_anything_further(isolated_env, monkeypatch):
    monkeypatch.setattr(ngt_dags, "CYCLE_SLEEP_SECONDS", 0)
    triggered = []
    monkeypatch.setattr(ngt_dags, "_trigger_process_dag", lambda *a, **k: triggered.append((a, k)))

    context = _context({"run_number": "398600", "cycle": 0})
    context["ti"].xcom_push(key="is_final", value=True)

    ngt_dags._make_advance_callable(4, "EcalPedestals")(**context)

    assert triggered == []


def test_upload_conditions_callable_invokes_step4_upload(isolated_env, job_runner):
    from ngt_calibration_loop import step4 as step4_lib

    run_dir = isolated_env.data_dir / "EcalPedestals" / "run398600"
    run_dir.mkdir(parents=True)
    ctx = step4_lib.build_run_context("EcalPedestals", "398600")
    job_dir = run_dir / "alcaPromptJob000"
    job_dir.mkdir(parents=True)
    (job_dir / "ecalPedsStep3_job.txt").touch()
    (job_dir / "PromptCalibProdEcalPedestals.root").write_bytes(b"fake")

    context = _context({"run_number": "398600", "cycle": 0})
    ngt_dags._make_check_and_batch_callable(4, "EcalPedestals")(**context)
    assert context["ti"].xcom_pull(key="action") == "batch"

    ngt_dags._make_upload_conditions_callable("EcalPedestals")(**context)

    assert len(job_runner) == 1
    assert job_runner[0]["script_name"] == "UPLOAD.sh"
