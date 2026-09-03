"""
Tests for NGTLoopStep4's FSM logic: discovering new run directories, recursively
discovering Step 3 ALCARECO outputs across alcaPromptJobNNN subdirectories, the
"reprocess everything, not just what's new" harvesting behavior (unlike Step 2/3),
and the harvesting job prep/launch/cleanup cycle. All external dependencies (EOS,
CMSSW, condDB upload) are mocked -- see conftest.py.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import NGTLoopStep4

CALIBRATION = "EcalPedestals"
WITNESS_NAME = "ecalPedsStep3_job.txt"
ROOT_NAME = "PromptCalibProdEcalPedestals.root"


@pytest.fixture
def loop(isolated_env):
    return NGTLoopStep4.NGTLoopStep4("Step4Test", CALIBRATION)


def _run_dir(loop, run_number):
    return Path(loop.pathWhereFilesAppear) / f"run{run_number}"


def _seed_step3_job_output(run_dir, job_name, create_root_file=True):
    """Create a Step 3 ALCA job's output the way NGTLoopStep3's
    PrepareAlCaPromptJobs/LaunchAlCaPromptJobs would have."""
    job_dir = run_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / WITNESS_NAME).touch()
    if create_root_file:
        (job_dir / ROOT_NAME).write_bytes(b"fake alcareco root file")
    return job_dir


# --- Construction / config wiring ---------------------------------------------------


def test_initial_state_and_config(loop, isolated_env):
    assert loop.state == "NotRunning"
    assert loop.calibration_name == CALIBRATION
    assert loop.pathWhereFilesAppear == str(isolated_env.data_dir / CALIBRATION) + "/"
    assert loop.CMSSWPath == "/nfshome0/sakura/"  # from calibrationYAML, untouched by path config


def test_cond_auth_path_is_redirected_via_config(loop, isolated_env):
    assert loop.condAuthPath == str(isolated_env.cond_auth_dir)
    assert os.environ["COND_AUTH_PATH"] == str(isolated_env.cond_auth_dir)


# --- Run discovery --------------------------------------------------------------------


def test_no_calibration_dir_yet_stays_not_running(loop):
    loop.TryLookForRun()
    assert loop.state == "NotRunning"


def test_finds_new_run_directory(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)

    loop.TryLookForRun()

    assert loop.state == "WaitingForFiles"
    assert loop.runNumber == "398600"
    assert Path(loop.workingDir) == run_dir


def test_ignores_already_processed_runs(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.setOfRunsProcessed.add("run398600")

    loop.TryLookForRun()

    assert loop.state == "NotRunning"


# --- Step 3 file discovery (recursive, across alcaPromptJobNNN dirs) ------------------


def test_check_files_for_processing_finds_files_recursively(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    loop.TryProcessFiles()

    assert loop.state == "CheckingFilesForProcess"
    assert len(loop.setOfFilesToProcess) == 1
    (only_file,) = loop.setOfFilesToProcess
    assert only_file == run_dir / "alcaPromptJob000" / ROOT_NAME


def test_witness_file_without_matching_root_file_is_dropped_at_prepare_time(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000", create_root_file=False)

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()

    assert loop.setOfInputFiles == set()


def test_reprocesses_all_files_not_just_new_ones(loop, popen_calls):
    """Distinguishing behavior vs. Step2/Step3: every time a new ALCARECO file
    shows up, ALL previously-harvested files are queued again too, since more
    statistics improve the payload even for already-uploaded conditions."""
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    # First harvesting round: only job000's output exists.
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()
    loop.TryLaunchHarvestingJobs()
    assert loop.setOfFilesProcessed == {run_dir / "alcaPromptJob000" / ROOT_NAME}
    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()
    assert loop.state == "WaitingForFiles"

    # A second ALCA job appears.
    _seed_step3_job_output(run_dir, "alcaPromptJob001")

    loop.TryProcessFiles()
    # setOfFilesToProcess is reset to *everything* available, not just job001's file.
    assert loop.setOfFilesToProcess == {
        run_dir / "alcaPromptJob000" / ROOT_NAME,
        run_dir / "alcaPromptJob001" / ROOT_NAME,
    }

    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()

    assert loop.setOfInputFiles == {
        run_dir / "alcaPromptJob000" / ROOT_NAME,
        run_dir / "alcaPromptJob001" / ROOT_NAME,
    }


def test_no_new_files_run_ended_moves_to_final(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    (run_dir / "runEnd.log").touch()

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "PreparingFinalFiles"
    assert loop.preparedFinalFiles is True


def test_no_new_files_run_ongoing_time_left_stays_waiting(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "WaitingForFiles"


def test_no_new_files_timeout_expired_moves_to_final(loop):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    loop.startTime = datetime.now(timezone.utc) - timedelta(hours=9)  # > timeoutInSeconds (8h)

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()

    assert loop.state == "PreparingFinalFiles"


# --- Harvesting job prep / launch / cleanup -------------------------------------------


def test_prepare_and_launch_harvesting_job_writes_metadata_and_script(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFiles"

    loop.TryPrepareHarvestingJobs()
    assert loop.state == "PreparingHarvestingJobs"
    job_dir = Path(loop.jobDir)
    assert job_dir == run_dir / "harvestJob000"

    metadata = json.loads((job_dir / "NGTCalibEcalPedestals.txt").read_text(encoding="utf-8"))
    assert metadata["since"] == "398600"
    assert metadata["inputTag"] == "EcalPedestals_NGTDemonstrator"
    assert metadata["destinationDatabase"] == "oracle://cms_orcon_prod/CMS_CONDITIONS"

    script = (job_dir / "HARVESTING.sh").read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep4" in script
    assert "ALCAHARVEST:EcalPedestals" in script
    assert "uploadConditions.py NGTCalibEcalPedestals.db" in script

    loop.TryLaunchHarvestingJobs()
    assert loop.state == "LaunchingHarvestingJobs"
    assert len(popen_calls) == 1
    assert popen_calls[0]["cmd"] == ["bash", "HARVESTING.sh"]
    assert popen_calls[0]["kwargs"]["cwd"] == loop.jobDir


def test_beamspot_metadata_has_no_since(isolated_env, popen_calls):
    loop = NGTLoopStep4.NGTLoopStep4("Step4Test", "BeamSpot")
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    job_dir = run_dir / "alcaPromptJob000"
    job_dir.mkdir(parents=True)
    (job_dir / "beamSpotStep3_job.txt").touch()
    (job_dir / "PromptCalibProdBeamSpotHP.root").write_bytes(b"fake")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()

    metadata = json.loads((Path(loop.jobDir) / "NGTCalibBeamSpot.txt").read_text(encoding="utf-8"))
    assert metadata["since"] is None


def test_no_launch_when_nothing_to_process(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    (run_dir / "runEnd.log").touch()

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()
    loop.TryLaunchHarvestingJobs()

    assert popen_calls == []
    assert loop.jobDir == "/dev/null"


def test_second_harvesting_job_gets_next_job_number(loop, popen_calls):
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()
    loop.TryLaunchHarvestingJobs()
    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()

    _seed_step3_job_output(run_dir, "alcaPromptJob001")
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    loop.TryPrepareHarvestingJobs()

    assert Path(loop.jobDir) == run_dir / "harvestJob001"


def test_cleanup_writes_summary_log_and_marks_run_processed_when_final(loop, popen_calls):
    """Two-cycle scenario: a Step3 file is harvested while the run is still
    ongoing (not final), then the run ends with nothing new to add, triggering
    final cleanup covering everything harvested so far."""
    run_dir = _run_dir(loop, 398600)
    run_dir.mkdir(parents=True)
    loop.TryLookForRun()
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    # Cycle 1: file arrives while the run is still ongoing -> harvested normally.
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFiles"
    loop.TryPrepareHarvestingJobs()
    loop.TryLaunchHarvestingJobs()
    loop.ContinueToCleanup()
    loop.ContinueAfterCleanup()
    assert loop.state == "WaitingForFiles"
    assert loop.preparedFinalFiles is False

    # Cycle 2: the run ends with nothing new -> final cleanup.
    (run_dir / "runEnd.log").touch()
    loop.TryProcessFiles()
    loop.ContinueAfterCheckFiles()
    assert loop.state == "PreparingFinalFiles"
    loop.TryPrepareHarvestingJobs()
    loop.TryLaunchHarvestingJobs()
    loop.ContinueToCleanup()

    summary = (run_dir / "allStep3FilesProcessed.log").read_text(encoding="utf-8")
    assert str(run_dir / "alcaPromptJob000" / ROOT_NAME) in summary

    loop.ContinueAfterCleanup()

    assert loop.state == "NotRunning"
    assert "run398600" in loop.setOfRunsProcessed
