"""Tests for airflow_automation/airflow_dags/ngt_dags_watch.py + its triggers -- the AssetWatcher DAG
design: all-static assets driven by AssetWatchers, zero cron DAGs. See that
module's docstring and watch_asset_scheduling_design.md.

DagBag wiring integrity + direct unit tests of the watcher triggers' async
run() and the prime_run / process callables, with OMS / EOS / job-launching
and the AssetStateStore all faked. The step2->3->4 callables come from
airflow_dags._process_tasks and are covered here too, via the
`run_and_file_from_context` seam and a chain smoke test that drives one file
through all four of them.

Guarded by pytest.importorskip("airflow"), so it's skipped wherever
apache-airflow isn't installed and the rest of the suite still runs.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("airflow")

import omsapi  # noqa: E402
from airflow.models import DagBag  # noqa: E402
from airflow.sdk.exceptions import AirflowSkipException  # noqa: E402

AIRFLOW_DAGS_DIR = Path(__file__).resolve().parents[2] / "airflow_automation" / "airflow_dags"
if str(AIRFLOW_DAGS_DIR.parent) not in sys.path:  # so `import airflow_dags` resolves
    sys.path.insert(0, str(AIRFLOW_DAGS_DIR.parent))

import airflow_dags.ngt_dags_watch as watch  # noqa: E402
from airflow_dags import _process_tasks as pp  # noqa: E402
from airflow_dags import triggers as trig  # noqa: E402
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


# --- fakes -------------------------------------------------------------------------


class _FakeAccessor:
    def __init__(self, store):
        self._store = store

    def get(self, key, default=None):
        return self._store.get(key, default)

    def set(self, key, value):
        self._store[key] = value

    def delete(self, key):
        self._store.pop(key, None)


class FakeAssetStateStore:
    """Stand-in for the AssetStateStoreAccessors the triggerer injects onto a
    BaseEventTrigger -- subscript by an Asset, get a per-asset accessor."""

    def __init__(self):
        self.by_name = {}

    def __getitem__(self, asset):
        name = getattr(asset, "name", asset)
        return _FakeAccessor(self.by_name.setdefault(name, {}))


def _drive(trigger, timeout=1.5, max_events=6):
    """Run trigger.run() to completion (or timeout). Returns the list of
    yielded payloads, or None if run() never returned within `timeout`
    (i.e. it's still polling -- the "nothing to do" outcome)."""

    async def _collect():
        out = []
        async for event in trigger.run():
            out.append(event.payload)
            if len(out) >= max_events:
                break
        return out

    try:
        return asyncio.run(asyncio.wait_for(_collect(), timeout))
    except asyncio.TimeoutError:
        return None


def _drive_restarting(trigger, rounds=6, timeout=1.5):
    """Call trigger.run() up to `rounds` times (the triggerer auto-restarts a
    watcher after each yield). Returns the concatenated payloads; stops early
    on a round that yields nothing / times out."""
    payloads = []
    for _ in range(rounds):
        got = _drive(trigger, timeout=timeout)
        if not got:  # None (timed out) or [] (returned without yielding)
            break
        payloads.extend(got)
    return payloads


def _seed_run(calibration, run_number, minutes_ago=5, end_time=None, last_ls=100):
    start_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    omsapi.configure_runs([make_run(run_number, start_time=start_time, end_time=end_time, last_ls=last_ls)])
    latched = step2_lib.find_new_run(calibration)
    assert latched == run_number
    return step2_lib.build_run_context(calibration, run_number)


def make_triggering(payload):
    ev = SimpleNamespace(
        extra={"from_trigger": True, "payload": dict(payload)},
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    return {"ngt://some/asset": [ev]}


# --- DAG wiring ------------------------------------------------------------------


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder=str(AIRFLOW_DAGS_DIR))


def test_dagbag_has_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_all_watch_dags_present_and_asset_scheduled(dagbag):
    expected = {watch.RUN_PRIMER_DAG_ID} | {watch._process_dag_id(c) for c in CALIBRATIONS}
    assert expected <= set(dagbag.dags)
    for dag_id in expected:
        assert type(dagbag.dags[dag_id].timetable).__name__ == "AssetTriggeredTimetable"


def _sole_scheduled_asset(dag):
    # schedule=[Asset(...)] -> AssetTriggeredTimetable(AssetAll(Asset(...)))
    objects = dag.timetable.asset_condition.objects
    assert len(objects) == 1
    return objects[0]


def test_run_primer_watches_ngt_runs(dagbag):
    dag = dagbag.dags[watch.RUN_PRIMER_DAG_ID]
    assert [t.task_id for t in dag.topological_sort()] == ["prime_run"]
    asset = _sole_scheduled_asset(dag)
    assert asset.uri.rstrip("/") == "ngt://runs"
    assert [w.name for w in asset.watchers] == ["ngt_run_watch"]
    assert type(asset.watchers[0].trigger).__name__ == "RunWatcherTrigger"


def test_process_dags_watch_their_per_calibration_file_asset(dagbag):
    for calibration in CALIBRATIONS:
        dag = dagbag.dags[watch._process_dag_id(calibration)]
        assert [t.task_id for t in dag.topological_sort()] == [
            "step2_express", "step3_alca", "step4_harvest", "step4_upload",
        ]
        asset = _sole_scheduled_asset(dag)
        assert asset.uri.rstrip("/") == f"ngt://files/{calibration.lower()}"
        assert len(asset.watchers) == 1
        watcher_trigger = asset.watchers[0].trigger
        assert type(watcher_trigger).__name__ == "LumisectionFileWatcherTrigger"
        assert watcher_trigger.calibration == calibration
        # retry policy comes from _process_tasks.LAUNCH_RETRY_KWARGS/UPLOAD_RETRY_KWARGS
        assert dag.get_task("step2_express").retries == 3
        assert dag.get_task("step4_upload").retries == 5


def test_no_cron_dag_in_this_design(dagbag):
    for dag_id in {watch.RUN_PRIMER_DAG_ID} | {watch._process_dag_id(c) for c in CALIBRATIONS}:
        tt = dagbag.dags[dag_id].timetable
        assert getattr(tt, "delta", None) is None  # not a DeltaDataIntervalTimetable / cron


# --- RunWatcherTrigger -----------------------------------------------------------


def test_run_watcher_yields_for_a_new_run(isolated_env):
    omsapi.configure_runs([make_run(398601, start_time=datetime.now(timezone.utc) - timedelta(minutes=5))])
    t = trig.RunWatcherTrigger(poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()

    events = _drive(t)

    assert events is not None and len(events) == 1
    assert events[0]["run_number"] == 398601
    assert "run_start_time" in events[0]
    assert "398601" in t.asset_state_store.by_name[trig.RUNS_ASSET_NAME]["seen_runs"]


def test_run_watcher_skips_a_run_already_in_the_state_store(isolated_env):
    omsapi.configure_runs([make_run(398601, start_time=datetime.now(timezone.utc) - timedelta(minutes=5))])
    t = trig.RunWatcherTrigger(poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()
    t.asset_state_store.by_name[trig.RUNS_ASSET_NAME] = {"seen_runs": ["398601"]}

    assert _drive(t) is None  # keeps polling, never yields


def test_run_watcher_works_without_a_state_store_falling_back_to_disk(isolated_env):
    omsapi.configure_runs([make_run(398601, start_time=datetime.now(timezone.utc) - timedelta(minutes=5))])
    t = trig.RunWatcherTrigger(poll_interval=0.01)  # no asset_state_store set

    events = _drive(t)

    assert events is not None and events[0]["run_number"] == 398601
    fallback = isolated_env.data_dir / "_ngt_watch_runs_seen.log"
    assert fallback.exists() and "398601" in fallback.read_text()


def test_run_watcher_idle_when_no_run(isolated_env):
    omsapi.configure_runs([])
    t = trig.RunWatcherTrigger(poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()
    assert _drive(t) is None


# --- LumisectionFileWatcherTrigger ---------------------------------------------


def test_file_watcher_yields_exactly_one_event_per_run_invocation(isolated_env, fake_eos):
    """Both files present at once must NOT be yielded in one run() call --
    Airflow coalesces same-instant asset events into one DagRun."""
    ctx = _seed_run("EcalPedestals", 398601)
    for ls in (51, 52):
        fake_eos.add_file(f"{ctx.path_where_files_appear}/run398601_ls00{ls}.root", run_number=398601, ls_numbers=[ls])

    t = trig.LumisectionFileWatcherTrigger("EcalPedestals", poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()

    first = _drive(t)
    assert first is not None and len(first) == 1  # exactly one, then run() returns
    assert Path(first[0]["file"]).name == "run398601_ls0051.root"

    # the triggerer would restart it; the next scan hands out the second file
    all_events = [first[0]] + (_drive(t) or [])
    assert {Path(e["file"]).name for e in all_events} == {"run398601_ls0051.root", "run398601_ls0052.root"}
    emitted = t.asset_state_store.by_name[trig.files_asset_name("EcalPedestals")]["emitted_run398601"]
    assert len(emitted) == 2


def test_file_watcher_skips_files_already_emitted(isolated_env, fake_eos):
    ctx = _seed_run("EcalPedestals", 398601)
    f1 = f"{ctx.path_where_files_appear}/run398601_ls0051.root"
    f2 = f"{ctx.path_where_files_appear}/run398601_ls0052.root"
    fake_eos.add_file(f1, run_number=398601, ls_numbers=[51])
    fake_eos.add_file(f2, run_number=398601, ls_numbers=[52])

    t = trig.LumisectionFileWatcherTrigger("EcalPedestals", poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()
    t.asset_state_store.by_name[trig.files_asset_name("EcalPedestals")] = {"emitted_run398601": [str(Path(f1))]}

    events = _drive(t)

    assert events is not None and len(events) == 1
    assert Path(events[0]["file"]).name == "run398601_ls0052.root"


def test_file_watcher_finalizes_on_check_ls_final(isolated_env, fake_eos, monkeypatch):
    ctx = _seed_run("EcalPedestals", 398601)
    monkeypatch.setattr(
        step2_lib,
        "check_ls_for_processing",
        lambda _ctx: step2_lib.CycleDecision(action="final", ls_to_process=set(), still_have_time=False),
    )
    t = trig.LumisectionFileWatcherTrigger("EcalPedestals", poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()
    t.asset_state_store.by_name[trig.files_asset_name("EcalPedestals")] = {"emitted_run398601": ["/x.root"]}

    events = _drive(t)

    assert events == []  # run() returned, nothing yielded
    assert (ctx.working_dir / step2_lib.RUN_END_LOG_NAME).exists()
    assert "emitted_run398601" not in t.asset_state_store.by_name[trig.files_asset_name("EcalPedestals")]


def test_file_watcher_finalizes_when_oms_run_end_grace_expires(isolated_env, fake_eos):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398601, start_time=now - timedelta(minutes=5), end_time=now - timedelta(seconds=3), last_ls=100)]
    )
    run_number = step2_lib.find_new_run("EcalPedestals")
    ctx = step2_lib.build_run_context("EcalPedestals", run_number)
    # sanity: check_ls_for_processing itself would NOT finalize this yet
    assert step2_lib.check_ls_for_processing(ctx).action == "wait"

    t = trig.LumisectionFileWatcherTrigger("EcalPedestals", poll_interval=0.01, run_end_grace_seconds=1)
    t.asset_state_store = FakeAssetStateStore()

    assert _drive(t) == []
    assert (ctx.working_dir / step2_lib.RUN_END_LOG_NAME).exists()


def test_file_watcher_idle_when_no_active_run_dir(isolated_env):
    t = trig.LumisectionFileWatcherTrigger("EcalPedestals", poll_interval=0.01)
    t.asset_state_store = FakeAssetStateStore()
    assert _drive(t) is None


def test_file_watcher_active_run_ignores_a_finalized_run_dir(isolated_env):
    ctx = _seed_run("EcalPedestals", 398601)
    (ctx.working_dir / step2_lib.RUN_END_LOG_NAME).touch()
    t = trig.LumisectionFileWatcherTrigger("EcalPedestals")
    assert t._active_run() is None


# --- run primer callable ------------------------------------------------------


def test_prime_run_creates_working_dirs_for_all_calibrations(isolated_env):
    omsapi.configure_runs([make_run(398601, start_time=datetime.now(timezone.utc) - timedelta(minutes=5))])

    watch._prime_run(**{"triggering_asset_events": make_triggering({"run_number": "398601"})})

    for calibration in CALIBRATIONS:
        run_dir = isolated_env.data_dir / calibration / "run398601"
        assert (run_dir / step2_lib.RUN_START_LOG_NAME).exists()


def test_prime_run_is_idempotent(isolated_env):
    omsapi.configure_runs([make_run(398601, start_time=datetime.now(timezone.utc) - timedelta(minutes=5))])
    ctx = {"triggering_asset_events": make_triggering({"run_number": "398601"})}
    watch._prime_run(**ctx)
    watch._prime_run(**ctx)  # must not raise on the second call


# --- run_and_file_from_context (the _process_tasks input seam) ---------------


def test_run_and_file_from_context_reads_dag_run_conf():
    ctx = {"dag_run": SimpleNamespace(conf={"run_number": "398601", "file": "/eos/x.root"})}
    assert pp.run_and_file_from_context(ctx) == ("398601", "/eos/x.root")


def test_run_and_file_from_context_reads_triggering_asset_event():
    ctx = {"triggering_asset_events": make_triggering({"run_number": "398601", "file": "/eos/x.root"})}
    assert pp.run_and_file_from_context(ctx) == ("398601", "/eos/x.root")


def test_run_and_file_from_context_skips_when_neither_present():
    with pytest.raises(AirflowSkipException):
        pp.run_and_file_from_context({"dag_run": SimpleNamespace(conf={})})


# --- thin chain smoke test through the shared _process_tasks callables ------


def test_watch_process_chain_runs_one_file_from_an_asset_event(isolated_env, fake_eos, job_runner):
    ctx2 = _seed_run("EcalPedestals", 398601)
    input_file = f"{ctx2.path_where_files_appear}/run398601_ls0051.root"
    fake_eos.add_file(input_file, run_number=398601, ls_numbers=[51])

    context = {
        "ti": _FakeTI(),
        "triggering_asset_events": make_triggering({"run_number": "398601", "file": input_file}),
    }

    pp.make_step2_express_callable("EcalPedestals")(**context)
    step2_output = context["ti"].xcom_pull(key="step2_output")
    assert step2_output is not None and len(job_runner) == 1
    Path(step2_output).write_bytes(b"(FAKE) RECO output\n")

    pp.make_step3_alca_callable("EcalPedestals")(**context)
    alca_job_dir = next(p for p in ctx2.working_dir.iterdir() if p.name.startswith("alcaPromptJob_"))
    (alca_job_dir / "ecalPedsStep3_job.txt").touch()
    (alca_job_dir / "PromptCalibProdEcalPedestals.root").write_bytes(b"(FAKE) ALCARECO output\n")

    pp.make_step4_harvest_callable("EcalPedestals")(**context)
    pp.make_step4_upload_callable("EcalPedestals")(**context)
    assert len(job_runner) == 4
    assert job_runner[-1]["script_name"] == "UPLOAD.sh"
    assert (ctx2.working_dir / step2_lib.PROCESSED_LOG_NAME).exists()


def test_watch_process_step2_skips_an_already_processed_file(isolated_env, fake_eos, job_runner):
    ctx2 = _seed_run("EcalPedestals", 398601)
    input_file = f"{ctx2.path_where_files_appear}/run398601_ls0051.root"
    fake_eos.add_file(input_file, run_number=398601, ls_numbers=[51])
    (ctx2.working_dir / step2_lib.PROCESSED_LOG_NAME).write_text(str(Path(input_file)) + "\n", encoding="utf-8")

    context = {
        "ti": _FakeTI(),
        "triggering_asset_events": make_triggering({"run_number": "398601", "file": input_file}),
    }
    with pytest.raises(AirflowSkipException):
        pp.make_step2_express_callable("EcalPedestals")(**context)
    assert len(job_runner) == 0


def test_step4_harvest_skips_when_nothing_new(isolated_env):
    run_dir = isolated_env.data_dir / "EcalPedestals" / "run398601"
    run_dir.mkdir(parents=True)
    context = {
        "ti": _FakeTI(),
        "triggering_asset_events": make_triggering({"run_number": "398601", "file": "irrelevant"}),
    }
    with pytest.raises(AirflowSkipException):
        pp.make_step4_harvest_callable("EcalPedestals")(**context)


class _FakeTI:
    def __init__(self):
        self._d = {}

    def xcom_push(self, key, value):
        self._d[key] = value

    def xcom_pull(self, task_ids=None, key=None):
        return self._d.get(key)
