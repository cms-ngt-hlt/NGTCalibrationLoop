"""Tests for ngt_calibration_loop.step2: run latching against OMS, incremental
LS discovery against EOS, the maxLatchTime/RunEnded escape hatches, and the
express job prep/launch/finalize cycle. All external services are mocked --
see conftest.py.

This is a 1:1 behavioral port of the old tests/test_step2.py (which drove
NGTLoopStep2's transitions FSM); every case there has an equivalent here
exercising the plain-function ngt_calibration_loop.step2 API instead.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import omsapi
import pytest

from ngt_calibration_loop import config, oms, shell, step2

CALIBRATION = "EcalPedestals"  # minLsToProcess=50 per calibrationYAML/EcalPedestals.yaml


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


# --- Config wiring -----------------------------------------------------------------


def test_config_wiring(isolated_env):
    calib_config = config.load_calibration_config(CALIBRATION)
    assert calib_config["step_2_config"]["minLsToProcess"] == 50
    ngt_params = config.load_ngt_parameters()
    assert ngt_params["DATA_BASE_PATH"] == str(isolated_env.data_dir)


# --- Run latching --------------------------------------------------------------------


def test_no_matching_oms_runs_returns_none(isolated_env):
    omsapi.configure_runs([])
    assert step2.find_new_run(CALIBRATION) is None


def test_latches_onto_live_run(isolated_env):
    start_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    omsapi.configure_runs([make_run(398600, start_time=start_time, end_time=None)])

    run_number = step2.find_new_run(CALIBRATION)

    assert run_number == 398600
    run_dir = Path(isolated_env.data_dir) / CALIBRATION / "run398600"
    assert run_dir.is_dir()
    assert (run_dir / "runStart.log").read_text(encoding="utf-8") == start_time.isoformat()

    ctx = step2.build_run_context(CALIBRATION, run_number)
    assert ctx.run_start_time == start_time
    assert "398/600/00000" in ctx.path_where_files_appear


def test_skips_run_that_already_has_a_working_dir(isolated_env):
    start_time = datetime.now(timezone.utc) - timedelta(hours=1)
    (Path(isolated_env.data_dir) / CALIBRATION / "run398600").mkdir(parents=True)
    omsapi.configure_runs([make_run(398600, start_time=start_time)])

    assert step2.find_new_run(CALIBRATION) is None


def test_skips_ended_run_below_min_ls_threshold(isolated_env):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398601, start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1), last_ls=5)]
    )  # 5 < minLsToProcess (50)

    assert step2.find_new_run(CALIBRATION) is None


def test_latches_onto_ended_run_with_enough_ls(isolated_env):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398602, start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1), last_ls=200)]
    )

    assert step2.find_new_run(CALIBRATION) == 398602


def test_picks_earliest_of_several_eligible_runs(isolated_env):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [
            make_run(398610, start_time=now - timedelta(hours=1), end_time=None),
            make_run(398605, start_time=now - timedelta(hours=2), end_time=None),
        ]
    )

    assert step2.find_new_run(CALIBRATION) == 398605  # earliest recent run, not the first in the list


def test_new_run_available_oms_outage_propagates(isolated_env):
    """find_new_run only guards q.data().json() against JSONDecodeError; a
    lower-level failure in q.data() itself (e.g. the OMS host being
    unreachable) is not caught and propagates. This documents that current
    behavior rather than asserting it's desirable."""
    omsapi.set_failure(True)
    with pytest.raises(RuntimeError):
        step2.find_new_run(CALIBRATION)


def test_daq_is_running_returns_false_on_oms_outage(isolated_env):
    run_number = _latch_and_get_run_number(398600)
    omsapi.set_failure(True)
    is_running, _last_ls = oms.daq_is_running(run_number)
    assert is_running is False


def test_last_ls_run_number_returns_zero_on_oms_outage():
    omsapi.set_failure(True)
    assert oms.last_ls_run_number(398600) == 0


def test_last_ls_run_number_returns_zero_when_run_not_found():
    """OMS responding successfully but with zero matching runs must not crash
    on an unguarded response["data"][0] -- it should behave like an outage."""
    omsapi.configure_runs([make_run(111111, start_time=datetime.now(timezone.utc))])
    assert oms.last_ls_run_number(398600) == 0


# --- LS discovery against (fake) EOS -----------------------------------------------


def test_check_ls_for_processing_discovers_new_files(isolated_env, fake_eos):
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    decision = step2.check_ls_for_processing(ctx)

    assert len(decision.ls_to_process) == 1
    assert decision.action == "batch"


def test_broken_eos_files_are_skipped(isolated_env, fake_eos):
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_broken_file(f"{directory}/run398600_ls0051_corrupt.root")

    decision = step2.check_ls_for_processing(ctx)

    assert decision.ls_to_process == set()
    assert decision.action == "wait"


def test_no_new_files_and_still_time_stays_waiting(isolated_env, fake_eos):
    ctx = _latch_live_run(398600)

    decision = step2.check_ls_for_processing(ctx)

    assert decision.action == "wait"


def test_enough_new_files_moves_to_batch(isolated_env, fake_eos):
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    decision = step2.check_ls_for_processing(ctx)

    assert decision.action == "batch"


# --- maxLatchTime / run-ended escape hatches ---------------------------------------


def test_expired_latch_time_forces_final_even_with_no_new_files(isolated_env, fake_eos):
    ctx = _latch_live_run(398600)
    ctx.run_start_time = datetime.now(timezone.utc) - timedelta(hours=9)  # > maxLatchTimeInHours (8)

    decision = step2.check_ls_for_processing(ctx)

    assert decision.action == "final"


def test_run_ended_and_files_caught_up_forces_final(isolated_env, fake_eos):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=now, last_ls=100)])

    run_number = step2.find_new_run(CALIBRATION)
    assert run_number == 398600
    ctx = step2.build_run_context(CALIBRATION, run_number)

    directory = ctx.path_where_files_appear
    path = f"{directory}/run398600_ls0100.root"
    fake_eos.add_file(path, run_number=398600, ls_numbers=[100])
    # Simulate this file having already been processed in an earlier cycle, so the
    # only thing left to detect this round is "the run has ended and FU caught up".
    (Path(ctx.working_dir) / step2.PROCESSED_LOG_NAME).write_text(path + "\n", encoding="utf-8")

    decision = step2.check_ls_for_processing(ctx)

    assert decision.action == "final"


# --- Express job prep / launch / finalize -------------------------------------------


def test_prepare_and_launch_express_job_full_cycle(isolated_env, fake_eos, job_runner):
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    decision = step2.check_ls_for_processing(ctx)
    assert decision.action == "batch"

    job_spec = step2.prepare_express_job(ctx, decision.ls_to_process)
    assert job_spec is not None
    script_path = Path(ctx.working_dir) / job_spec.script_name
    assert script_path.exists()
    script = script_path.read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep2" in script
    assert job_spec.output_file == "run398600_LS0051To0051_ecalPedsStep2.root"

    step2.run_express_job(job_spec)
    assert len(job_runner) == 1
    assert job_runner[0]["script_name"] == job_spec.script_name
    assert job_runner[0]["cwd"] == str(ctx.working_dir)

    is_final = step2.finalize_cycle(ctx, job_spec, decision)
    assert is_final is False  # not the final batch, run keeps going

    processed = step2.load_already_processed(ctx.working_dir)
    assert any("run398600_ls0051.root" in p for p in processed)


def test_max_files_per_job_batches_one_at_a_time(isolated_env, fake_eos):
    """MAXIMUM_LS_PER_JOB is 1: with two new LS files available, only the
    lexicographically-first is included in a single express job; the other is
    left for a later cycle's check_ls_for_processing to pick up (it isn't
    marked processed, so it isn't lost)."""
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])
    fake_eos.add_file(f"{directory}/run398600_ls0052.root", run_number=398600, ls_numbers=[52])

    decision = step2.check_ls_for_processing(ctx)
    assert len(decision.ls_to_process) == 2

    job_spec = step2.prepare_express_job(ctx, decision.ls_to_process)

    assert len(job_spec.ls_batch) == 1
    assert step2.load_already_processed(ctx.working_dir) == set()  # not marked processed yet


def test_finalize_writes_run_end_and_summary_logs_when_final(isolated_env, fake_eos, job_runner):
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])
    ctx.run_start_time = datetime.now(timezone.utc) - timedelta(hours=9)  # forces final

    decision = step2.check_ls_for_processing(ctx)
    assert decision.action == "final"

    job_spec = step2.prepare_express_job(ctx, decision.ls_to_process)
    step2.run_express_job(job_spec)
    is_final = step2.finalize_cycle(ctx, job_spec, decision)
    assert is_final is True

    working_dir = Path(ctx.working_dir)
    assert (working_dir / "runEnd.log").exists()
    all_processed = (working_dir / "allLSProcessed.log").read_text(encoding="utf-8")
    assert "run398600_ls0051.root" in all_processed
    expected_outputs = (working_dir / "expectedOutputs.log").read_text(encoding="utf-8")
    assert "run398600_LS0051To0051_ecalPedsStep2.root" in expected_outputs


def test_no_job_prepared_when_no_ls_to_process(isolated_env, fake_eos, job_runner):
    ctx = _latch_live_run(398600)
    ctx.run_start_time = datetime.now(timezone.utc) - timedelta(hours=9)

    decision = step2.check_ls_for_processing(ctx)
    job_spec = step2.prepare_express_job(ctx, decision.ls_to_process)

    assert job_spec is None
    assert job_runner == []


def test_run_express_job_raises_on_failure_for_airflow_retry(isolated_env, fake_eos, job_runner):
    """The whole point of the migration: a failed launch must raise so the
    calling Airflow task fails (and is retried), instead of the old
    Popen-and-forget behavior which never even checked the exit code."""
    ctx = _latch_live_run(398600)
    directory = ctx.path_where_files_appear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])
    decision = step2.check_ls_for_processing(ctx)
    job_spec = step2.prepare_express_job(ctx, decision.ls_to_process)

    job_runner.queue_failure()
    with pytest.raises(shell.JobScriptFailedError):
        step2.run_express_job(job_spec)


# --- helpers --------------------------------------------------------------------------


def _latch_and_get_run_number(run_number):
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(run_number, start_time=start_time, end_time=None)])
    found = step2.find_new_run(CALIBRATION)
    assert found == run_number
    return found


def _latch_live_run(run_number):
    # Deliberately left configured in omsapi (not reset) after latching: later
    # checks (e.g. run_ended_and_files_are_ready) query OMS again for this same
    # run number, matching how a live cycle keeps polling OMS throughout a run.
    _latch_and_get_run_number(run_number)
    return step2.build_run_context(CALIBRATION, run_number)
