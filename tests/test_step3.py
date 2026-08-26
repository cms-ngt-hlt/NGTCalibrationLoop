"""
Tests for NGTLoopStep3's FSM logic: discovering new run directories under
DATA_BASE_PATH, incrementally batching Step 2 output files via their witness
(*_job.txt) files, the RunIsNotComplete/StillHaveTime escape hatches, and the
ALCAPROMPT job prep/launch/cleanup cycle. Step 3 discovers files by walking the
real (tmp_path) filesystem, so only subprocess.Popen needs mocking -- see
conftest.py's `popen_calls` fixture.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import NGTLoopStep3

CALIBRATION = "EcalPedestals"
WITNESS_SUFFIX = "ecalPedsStep2_job.txt"
ROOT_SUFFIX = "ecalPedsStep2.root"


@pytest.fixture
def loop(isolated_env):
    return NGTLoopStep3.NGTLoopStep3("Step3Test", CALIBRATION)


def _run_dir(loop, run_number):
    return Path(loop.pathWhereFilesAppear) / f"run{run_number}"


def _seed_step2_output(run_dir, basename, create_root_file=True):
    """Create a Step 2 witness file (and, by default, its matching root file) the
    way NGTLoopStep2's PrepareExpressJobs/LaunchExpressJobs would have."""
    (run_dir / f"{basename}_{WITNESS_SUFFIX}").touch()
    if create_root_file:
        (run_dir / f"{basename}_{ROOT_SUFFIX}").write_bytes(b"fake root file")


# --- Construction / config wiring ---------------------------------------------------


def test_initial_state_and_config(loop, isolated_env):
    assert loop.state == "NotRunning"
    assert loop.calibration_name == CALIBRATION
    assert loop.pathWhereFilesAppear == str(isolated_env.data_dir / CALIBRATION) + "/"


# --- Run discovery (NotRunning -> WaitingForStep2Files) -----------------------------


def test_no_calibration_dir_yet_stays_not_running(loop):
    loop.TryLookForRun()
    assert loop.state == "NotRunning"


def test_finds_new_run_directory(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)

    loop.TryLookForRun()

    assert loop.state == "WaitingForStep2Files"
    assert loop.runNumber == "398600"
    # SetupNewRun concatenates pathWhereFilesAppear (already trailing-slashed) with
    # "/run..." verbatim, so the raw string has a doubled slash; Path() normalizes it.
    assert Path(loop.workingDir) == run_dir


def test_reads_run_start_time_from_runstart_log(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    start_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
    (run_dir / "runStart.log").write_text(start_time.isoformat(), encoding="utf-8")

    loop.TryLookForRun()

    assert loop.startTime == start_time


def test_defaults_start_time_to_now_without_runstart_log(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    before = datetime.now(timezone.utc)

    loop.TryLookForRun()

    assert before <= loop.startTime <= datetime.now(timezone.utc)


def test_ignores_already_processed_runs(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.setOfRunsProcessed.add("run398600")

    loop.TryLookForRun()

    assert loop.state == "NotRunning"


def test_picks_earliest_of_several_new_runs(loop):
    _run_dir(loop, 398700).mkdir(parents=True)
    _run_dir(loop, 398600).mkdir(parents=True)

    loop.TryLookForRun()

    assert loop.runNumber == "398600"


# --- Step 2 file discovery (witness-file driven) -------------------------------------


def test_check_files_for_processing_finds_new_witness_files(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    loop.TryProcessFiles()  # WaitingForStep2Files -> CheckingFilesForProcess

    assert loop.state == "CheckingFilesForProcess"
    assert len(loop.setOfFilesToProcess) == 1
    assert loop.waitingFiles is True
    assert loop.enoughFiles is True  # minimumFiles defaults to 1
    (only_file,) = loop.setOfFilesToProcess
    assert only_file.name == f"run398600_LS0051To0051_{ROOT_SUFFIX}"


def test_enough_new_files_moves_to_preparing_files(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "PreparingFiles"


def test_no_new_files_run_ongoing_time_left_stays_waiting(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "WaitingForStep2Files"


def test_no_new_files_run_ended_moves_to_final(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    (run_dir / "runEnd.log").touch()

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "PreparingFinalFiles"
    assert loop.preparedFinalFiles is True


def test_no_new_files_timeout_expired_moves_to_final_even_if_run_ongoing(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    loop.startTime = datetime.now(timezone.utc) - timedelta(hours=10)  # > timeoutInSeconds (9h)

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "PreparingFinalFiles"


def test_witness_file_without_matching_root_file_is_dropped_at_prepare_time(loop):
    """A witness file can appear before its root file is fully flushed to disk;
    PrepareFilesForProcessing re-checks existence and drops anything missing."""
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051", create_root_file=False)

    loop.TryProcessFiles()
    assert len(loop.setOfFilesToProcess) == 1  # witness file was seen...

    loop.ContinueAfterCheckFiles()
    loop.TryPrepareALCAPROMPTJobs()

    assert loop.setOfInputFiles == set()  # ...but its root file never existed


# --- ALCAPROMPT job prep / launch / cleanup ------------------------------------------


def test_prepare_and_launch_alcaprompt_job_full_cycle(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFiles"

    loop.TryPrepareALCAPROMPTJobs()
    assert loop.state == "PreparingAlCaPromptJobs"
    job_dir = Path(loop.jobDir)
    assert job_dir == run_dir / "alcaPromptJob000"
    script = (job_dir / "ALCAOUTPUT.sh").read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep3" in script
    assert "ALCA:PromptCalibProdEcalPedestals" in script
    assert loop.alcaJobNumber == 1

    loop.TryLaunchALCAPROMPTJobs()
    assert loop.state == "LaunchingAlCaPromptJobs"
    assert len(popen_calls) == 1
    assert popen_calls[0]["cmd"] == ["bash", "ALCAOUTPUT.sh"]
    assert popen_calls[0]["kwargs"]["cwd"] == loop.jobDir
    assert loop.setOfFilesToProcess == set()
    assert loop.setOfInputFiles == set()

    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()
    assert loop.state == "WaitingForStep2Files"  # not final -> keep watching this run


def test_second_alcaprompt_job_gets_next_job_number(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareALCAPROMPTJobs()
    loop.TryLaunchALCAPROMPTJobs()
    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()

    _seed_step2_output(run_dir, "run398600_LS0052To0052")
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareALCAPROMPTJobs()

    assert Path(loop.jobDir) == run_dir / "alcaPromptJob001"


def test_no_launch_when_nothing_to_process(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    (run_dir / "runEnd.log").touch()  # run ends with no Step 2 output at all

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFinalFiles"

    loop.TryPrepareALCAPROMPTJobs()
    loop.TryLaunchALCAPROMPTJobs()

    assert popen_calls == []
    assert loop.jobDir == "/dev/null"


def test_cleanup_writes_summary_log_and_marks_run_processed_when_final(loop, popen_calls):
    """Two-cycle scenario mirroring the real loop: a Step2 file is processed while
    the run is still ongoing (not final), then the run ends with nothing new left
    to process, which triggers final cleanup covering everything processed so far."""
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    # Cycle 1: file arrives while the run is still ongoing -> processed normally.
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFiles"
    loop.TryPrepareALCAPROMPTJobs()
    loop.TryLaunchALCAPROMPTJobs()
    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()
    assert loop.state == "WaitingForStep2Files"
    assert loop.preparedFinalFiles is False

    # Cycle 2: the run ends with no further Step2 files -> final cleanup.
    (run_dir / "runEnd.log").touch()
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFinalFiles"
    loop.TryPrepareALCAPROMPTJobs()
    loop.TryLaunchALCAPROMPTJobs()
    loop.ContinueToCleanup()

    summary = (run_dir / "allStep2FilesProcessed.log").read_text(encoding="utf-8")
    assert f"run398600_LS0051To0051_{ROOT_SUFFIX}" in summary

    loop.ContinueAfterCleanup()

    assert loop.state == "NotRunning"
    assert "run398600" in loop.setOfRunsProcessed
