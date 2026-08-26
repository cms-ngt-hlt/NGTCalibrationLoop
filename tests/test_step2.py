"""
Tests for NGTLoopStep2's FSM logic: run latching against OMS, incremental LS
discovery against EOS, the maxLatchTime/RunEnded escape hatches, and the express
job prep/launch/cleanup cycle. All external services are mocked -- see conftest.py.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import omsapi
import pytest

import NGTLoopStep2

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


@pytest.fixture
def loop(isolated_env):
    return NGTLoopStep2.NGTLoopStep2("Step2Test", CALIBRATION)


# --- Construction / config wiring -------------------------------------------------


def test_initial_state_and_config(loop, isolated_env):
    assert loop.state == "NotRunning"
    assert loop.calibration_name == CALIBRATION
    assert loop.dataBasePath == str(isolated_env.data_dir)
    assert loop.minLSToProcess == 50


# --- Run latching (NotRunning -> WaitingForLS) -------------------------------------


def test_no_matching_oms_runs_stays_not_running(loop):
    omsapi.configure_runs([])
    loop.TryStartRun()
    assert loop.state == "NotRunning"


def test_latches_onto_live_run(loop):
    start_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    omsapi.configure_runs([make_run(398600, start_time=start_time, end_time=None)])

    loop.TryStartRun()

    assert loop.state == "WaitingForLS"
    assert loop.runNumber == 398600
    assert loop.runStartTime == start_time
    assert "398/600/00000" in loop.pathWhereFilesAppear

    run_dir = Path(loop.dataBasePath) / CALIBRATION / "run398600"
    assert run_dir.is_dir()
    assert (run_dir / "runStart.log").read_text(encoding="utf-8") == start_time.isoformat()


def test_skips_run_that_already_has_a_working_dir(loop):
    start_time = datetime.now(timezone.utc) - timedelta(hours=1)
    (Path(loop.dataBasePath) / CALIBRATION / "run398600").mkdir(parents=True)
    omsapi.configure_runs([make_run(398600, start_time=start_time)])

    loop.TryStartRun()

    assert loop.state == "NotRunning"


def test_skips_ended_run_below_min_ls_threshold(loop):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398601, start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1), last_ls=5)]
    )  # 5 < minLsToProcess (50)

    loop.TryStartRun()

    assert loop.state == "NotRunning"


def test_latches_onto_ended_run_with_enough_ls(loop):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [make_run(398602, start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1), last_ls=200)]
    )

    loop.TryStartRun()

    assert loop.state == "WaitingForLS"
    assert loop.runNumber == 398602


def test_picks_earliest_of_several_eligible_runs(loop):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [
            make_run(398610, start_time=now - timedelta(hours=1), end_time=None),
            make_run(398605, start_time=now - timedelta(hours=2), end_time=None),
        ]
    )

    loop.TryStartRun()

    assert loop.runNumber == 398605  # earliest recent run, not the first in the list


def test_new_run_available_oms_outage_propagates(loop):
    """NewRunAvailable only guards q.data().json() against JSONDecodeError; a
    lower-level failure in q.data() itself (e.g. the OMS host being unreachable)
    is not caught and propagates. This documents that current behavior rather
    than asserting it's desirable -- unlike DAQIsRunning/LastLSRunNumber below,
    which do wrap the whole OMS call in a broad except."""
    omsapi.set_failure(True)
    with pytest.raises(RuntimeError):
        loop.TryStartRun()


def test_daq_is_running_returns_false_on_oms_outage(loop):
    _latch_live_run(loop, 398600)
    omsapi.set_failure(True)
    assert loop.DAQIsRunning() is False


def test_last_ls_run_number_returns_zero_on_oms_outage(loop):
    omsapi.set_failure(True)
    assert loop.LastLSRunNumber(398600) == 0


def test_last_ls_run_number_returns_zero_when_run_not_found(loop):
    """OMS responding successfully but with zero matching runs (e.g. the run
    isn't visible yet, or was queried under a stale/wrong run number) must not
    crash on an unguarded response["data"][0] -- it should behave like an outage."""
    omsapi.configure_runs([make_run(111111, start_time=datetime.now(timezone.utc))])
    assert loop.LastLSRunNumber(398600) == 0


# --- LS discovery against (fake) EOS -----------------------------------------------


def test_check_ls_for_processing_discovers_new_files(loop, fake_eos):
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    loop.TryProcessLS()  # WaitingForLS -> CheckingLSForProcess

    assert loop.state == "CheckingLSForProcess"
    assert len(loop.setOfLSToProcess) == 1
    assert loop.waitingLS is True
    assert loop.enoughLS is True  # minimumLS defaults to 1


def test_broken_eos_files_are_skipped(loop, fake_eos):
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_broken_file(f"{directory}/run398600_ls0051_corrupt.root")

    loop.TryProcessLS()

    assert loop.setOfLSToProcess == set()
    assert loop.waitingLS is False


def test_no_new_files_and_still_time_stays_waiting(loop, fake_eos):
    _latch_live_run(loop, 398600)

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()

    assert loop.state == "WaitingForLS"


def test_enough_new_files_moves_to_preparing_ls(loop, fake_eos):
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()

    assert loop.state == "PreparingLS"


# --- maxLatchTime / run-ended escape hatches ---------------------------------------


def test_expired_latch_time_forces_final_ls_even_with_no_new_files(loop, fake_eos):
    _latch_live_run(loop, 398600)
    loop.runStartTime = datetime.now(timezone.utc) - timedelta(hours=9)  # > maxLatchTimeInHours (8)

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()

    assert loop.state == "PreparingFinalLS"
    assert loop.preparedFinalLS is True


def test_run_ended_and_files_caught_up_forces_final_ls(loop, fake_eos):
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=now, last_ls=100)])
    loop.TryStartRun()
    assert loop.state == "WaitingForLS"

    directory = loop.pathWhereFilesAppear
    path = f"{directory}/run398600_ls0100.root"
    fake_eos.add_file(path, run_number=398600, ls_numbers=[100])
    # Simulate this file having already been processed in an earlier cycle, so the
    # only thing left to detect this round is "the run has ended and FU caught up".
    loop.setOfLSProcessed.add(path)

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()

    assert loop.state == "PreparingFinalLS"


# --- Express job prep / launch / cleanup -------------------------------------------


def test_prepare_and_launch_express_job_full_cycle(loop, fake_eos, popen_calls):
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()
    assert loop.state == "PreparingLS"

    loop.TryPrepareExpressJobs()
    assert loop.state == "PreparingExpressJobs"
    assert loop.setOfExpressLS  # populated before being written out
    script_path = Path(loop.workingDir) / loop.tempScriptName
    assert script_path.exists()
    script = script_path.read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep2" in script
    assert "run398600_LS0051To0051_ecalPedsStep2.root" in script
    assert len(loop.setOfExpectedOutputs) == 1

    loop.TryLaunchExpressJobs()
    assert loop.state == "LaunchingExpressJobs"
    assert len(popen_calls) == 1
    assert popen_calls[0]["cmd"] == ["bash", loop.tempScriptName]
    assert popen_calls[0]["kwargs"]["cwd"] == loop.workingDir
    assert loop.setOfLSToProcess == set()

    loop.ContinueToCleanup()
    assert loop.state == "CleanupState"

    loop.ContinueAfterCleanup()
    assert loop.state == "WaitingForLS"  # not the final batch, run keeps going


def test_max_files_per_job_batches_one_at_a_time(loop, fake_eos):
    """maximumFilesPerJob defaults to 1: with two new LS files available, only the
    lexicographically-first is included in a single express job; the other is left
    in setOfLSToProcess's source set to be picked up on the next CheckLSForProcessing
    pass (it is not marked processed, so it isn't lost)."""
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])
    fake_eos.add_file(f"{directory}/run398600_ls0052.root", run_number=398600, ls_numbers=[52])

    loop.TryProcessLS()
    assert len(loop.setOfLSToProcess) == 2

    loop.ContinueAfterCheckLS()
    loop.TryPrepareExpressJobs()

    assert len(loop.setOfExpressLS) == 1
    assert loop.setOfLSToProcess == set()  # cleared for this cycle...
    assert loop.setOfLSProcessed == set()  # ...but not yet marked processed


def test_cleanup_writes_run_end_and_summary_logs_when_final(loop, fake_eos, popen_calls):
    _latch_live_run(loop, 398600)
    directory = loop.pathWhereFilesAppear
    fake_eos.add_file(f"{directory}/run398600_ls0051.root", run_number=398600, ls_numbers=[51])
    loop.runStartTime = datetime.now(timezone.utc) - timedelta(hours=9)  # forces PreparingFinalLS

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()
    assert loop.state == "PreparingFinalLS"

    loop.TryPrepareExpressJobs()
    loop.TryLaunchExpressJobs()
    loop.ContinueToCleanup()  # ExecuteCleanup (on_enter of CleanupState) writes the logs

    working_dir = Path(loop.workingDir)
    assert (working_dir / "runEnd.log").exists()

    loop.ContinueAfterCleanup()

    assert loop.state == "NotRunning"  # WePreparedFinalLS -> back to NotRunning
    all_processed = (working_dir / "allLSProcessed.log").read_text(encoding="utf-8")
    assert "run398600_ls0051.root" in all_processed
    expected_outputs = (working_dir / "expectedOutputs.log").read_text(encoding="utf-8")
    assert "run398600_LS0051To0051_ecalPedsStep2.root" in expected_outputs


def test_no_launch_when_no_ls_to_process(loop, fake_eos, popen_calls):
    _latch_live_run(loop, 398600)
    loop.runStartTime = datetime.now(timezone.utc) - timedelta(hours=9)

    loop.TryProcessLS()
    loop.ContinueAfterCheckLS()
    loop.TryPrepareExpressJobs()
    loop.TryLaunchExpressJobs()

    assert popen_calls == []


# --- helpers ------------------------------------------------------------------------


def _latch_live_run(loop, run_number):
    # Deliberately left configured in omsapi (not reset) after latching: later FSM
    # steps (e.g. RunHasEndedAndFilesAreReady) query OMS again for this same run
    # number, matching how the real daemon keeps polling OMS throughout a run.
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([make_run(run_number, start_time=start_time, end_time=None)])
    loop.TryStartRun()
    assert loop.state == "WaitingForLS"
