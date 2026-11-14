"""Tests for ngt_calibration_loop.step4: recursively discovering Step 3
ALCARECO outputs across alcaPromptJob* subdirectories, the "reprocess
everything, not just what's new" harvesting behavior (unlike Step 2/3), and
the harvesting job prep / run / upload / finalize cycle -- including the split
between run_harvesting_job and upload_conditions as two independently-retryable
steps.

1:1 behavioral port of the old tests/fsm-loops/test_step4.py.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from support import stand_in_cmssw

from ngt_calibration_loop import shell, step4

CALIBRATION = "EcalPedestals"
WITNESS_NAME = "ecalPedsStep3_job.txt"
ROOT_NAME = "PromptCalibProdEcalPedestals.root"


def _patch_calib_yaml(isolated_env, calibration, **step_4_config_overrides):
    """Merge `step_4_config_overrides` into the copied calibrationYAML/
    {calibration}.yaml -- e.g. timeoutSeconds, same as scenario-player/seed.py's
    cmd_setup patches it for the live-test setup (see step4.py's
    DEFAULT_TIMEOUT_SECONDS)."""
    path = isolated_env.calib_yaml_dir / f"{calibration}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["step_4_config"].update(step_4_config_overrides)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _run_dir(isolated_env, run_number):
    return isolated_env.data_dir / CALIBRATION / f"run{run_number}"


def _seed_step3_job_output(run_dir, job_name, create_root_file=True):
    job_dir = run_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / WITNESS_NAME).touch()
    if create_root_file:
        (job_dir / ROOT_NAME).write_bytes(b"fake alcareco root file")
    return job_dir


# --- Config wiring --------------------------------------------------------------------


def test_cond_auth_path_is_redirected_via_config(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    assert ctx.cond_auth_path == str(isolated_env.cond_auth_dir)
    assert os.environ["COND_AUTH_PATH"] == str(isolated_env.cond_auth_dir)
    assert ctx.cmssw_path == "/nfshome0/sakura/"  # from calibrationYAML, untouched by path config


def test_build_run_context_points_at_the_run_directory(isolated_env):
    """(The FSM's run-directory scan, step4's find_new_run, is covered in
    test_original_fsm_only.py.)"""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    ctx = step4.build_run_context(CALIBRATION, "398600")

    assert Path(ctx.working_dir) == run_dir


# --- Step 3 file discovery (recursive, across alcaPromptJob* dirs) --------------------


def test_check_files_for_processing_finds_files_recursively(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    decision = step4.check_files_for_processing(ctx)

    assert decision.action == "batch"
    assert len(decision.files_to_process) == 1
    (only_file,) = decision.files_to_process
    assert only_file == run_dir / "alcaPromptJob000" / ROOT_NAME


def test_witness_file_without_matching_root_file_is_dropped_at_prepare_time(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000", create_root_file=False)

    decision = step4.check_files_for_processing(ctx)
    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)

    assert job_spec is None


def test_reprocesses_all_files_not_just_new_ones(isolated_env, job_runner):
    """Distinguishing behavior vs. Step2/Step3: every time a new ALCARECO file
    shows up, ALL previously-harvested files are queued again too, since more
    statistics improve the payload even for already-uploaded conditions."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    # First harvesting round: only job000's output exists.
    decision_1 = step4.check_files_for_processing(ctx)
    assert decision_1.action == "batch"
    job_spec_1 = step4.prepare_harvesting_job(ctx, decision_1.files_to_process)
    step4.run_harvesting_job(job_spec_1)
    step4.upload_conditions(job_spec_1)
    step4.finalize_cycle(ctx, job_spec_1, decision_1)
    assert job_spec_1.input_files == {run_dir / "alcaPromptJob000" / ROOT_NAME}

    # A second ALCA job appears.
    _seed_step3_job_output(run_dir, "alcaPromptJob001")

    decision_2 = step4.check_files_for_processing(ctx)
    # files_to_process is *everything* available, not just job001's file.
    assert decision_2.files_to_process == {
        run_dir / "alcaPromptJob000" / ROOT_NAME,
        run_dir / "alcaPromptJob001" / ROOT_NAME,
    }

    job_spec_2 = step4.prepare_harvesting_job(ctx, decision_2.files_to_process)
    assert job_spec_2.input_files == {
        run_dir / "alcaPromptJob000" / ROOT_NAME,
        run_dir / "alcaPromptJob001" / ROOT_NAME,
    }


def test_no_new_files_run_ended_moves_to_final(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    (run_dir / "runEnd.log").touch()

    decision = step4.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_no_new_files_run_ongoing_time_left_stays_waiting(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")

    decision = step4.check_files_for_processing(ctx)

    assert decision.action == "wait"


def test_no_new_files_timeout_expired_moves_to_final(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    ctx.start_time = datetime.now(timezone.utc) - timedelta(hours=9)  # > TIMEOUT_SECONDS (8h)

    decision = step4.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_timeout_seconds_is_configurable_per_calibration(isolated_env):
    """See test_step2_lib.py's equivalent test / step4.py's
    DEFAULT_TIMEOUT_SECONDS for why the live-test setup needs this."""
    _patch_calib_yaml(isolated_env, CALIBRATION, timeoutSeconds=5)

    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    assert ctx.timeout_seconds == 5

    # 1 minute old: past the tiny 5s override, but well inside the 8h
    # production default -- proves the override actually governs this.
    ctx.start_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    decision = step4.check_files_for_processing(ctx)
    assert decision.action == "final"


# --- Harvesting job prep / run / upload / finalize ------------------------------------


def test_prepare_writes_metadata_and_script(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    decision = step4.check_files_for_processing(ctx)
    assert decision.action == "batch"

    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)
    assert job_spec is not None
    assert job_spec.job_dir.parent == run_dir
    assert job_spec.job_dir.name.startswith("harvestJob_")

    metadata = json.loads((job_spec.job_dir / "NGTCalibEcalPedestals.txt").read_text(encoding="utf-8"))
    assert metadata["since"] == "398600"
    assert metadata["inputTag"] == "EcalPedestals_NGTDemonstrator"
    assert metadata["destinationDatabase"] == "oracle://cms_orcon_prod/CMS_CONDITIONS"

    script = (job_spec.job_dir / "HARVESTING.sh").read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep4" in script
    assert "ALCAHARVEST:EcalPedestals" in script
    assert "uploadConditions.py" not in script  # split out into upload_conditions()


def test_run_harvesting_job_then_upload_conditions_are_separate_calls(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    decision = step4.check_files_for_processing(ctx)
    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)

    step4.run_harvesting_job(job_spec)
    assert len(job_runner) == 1
    assert job_runner[0]["script_name"] == "HARVESTING.sh"

    step4.upload_conditions(job_spec)
    assert len(job_runner) == 2
    assert job_runner[1]["script_name"] == "UPLOAD.sh"
    upload_script = (job_spec.job_dir / "UPLOAD.sh").read_text(encoding="utf-8")
    assert f"uploadConditions.py {job_spec.final_db_name}" in upload_script


@pytest.mark.skipif(sys.platform == "win32", reason="runs HARVESTING.sh with bash")
@pytest.mark.parametrize(
    "cms_run, outcome",
    [
        (stand_in_cmssw.PAYLOAD, None),
        (stand_in_cmssw.EMPTY_PAYLOAD, step4.NoPayload),
        (stand_in_cmssw.NO_PAYLOAD_FILE, shell.JobScriptFailedError),
        (stand_in_cmssw.FAILS, shell.JobScriptFailedError),
    ],
)
def test_the_harvest_tells_an_empty_payload_from_a_failure(isolated_env, tmp_path, monkeypatch, cms_run, outcome):
    """HARVESTING.sh, run with bash under a stand-in CMSSW: an empty payload is no payload (the harvester found too
    few entries), and a failed cmsRun or a missing payload fails, where it used to exit 0 either way."""
    stand_in_cmssw.install(tmp_path / "bin", monkeypatch, cms_run)
    (tmp_path / "cmssw" / "CMSSW_16_0_7_patch1" / "src").mkdir(parents=True)
    _patch_calib_yaml(isolated_env, CALIBRATION, cmssw_base_path=f"{tmp_path / 'cmssw'}/")
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    job_spec = step4.prepare_harvesting_job(ctx, step4.check_files_for_processing(ctx).files_to_process)

    if outcome is None:
        step4.run_harvesting_job(job_spec)
        assert (job_spec.job_dir / job_spec.final_db_name).read_text(encoding="utf-8") == "payload\n"
    else:
        with pytest.raises(outcome):
            step4.run_harvesting_job(job_spec)
        assert not (job_spec.job_dir / job_spec.final_db_name).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="runs HARVESTING.sh with bash")
def test_the_harvest_fails_without_its_release_area_instead_of_running_elsewhere(isolated_env, tmp_path, monkeypatch):
    """Its `cd -` used to take a failed cd's job to whatever directory the caller had been in before."""
    stand_in_cmssw.install(tmp_path / "bin", monkeypatch, stand_in_cmssw.PAYLOAD)
    _patch_calib_yaml(isolated_env, CALIBRATION, cmssw_base_path=f"{tmp_path / 'no-such-area'}/")
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    job_spec = step4.prepare_harvesting_job(ctx, step4.check_files_for_processing(ctx).files_to_process)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("OLDPWD", str(elsewhere))

    with pytest.raises(shell.JobScriptFailedError):
        step4.run_harvesting_job(job_spec)
    assert not list(elsewhere.iterdir())


def test_the_upload_runs_the_command_the_parameters_name(isolated_env, job_runner):
    """A test setup names its stand-in by absolute path: after a real cmsenv, PATH finds the release's own."""
    params = json.loads(isolated_env.parameters_path.read_text(encoding="utf-8"))
    params["UPLOAD_COMMAND"] = "/opt/ngt/scenario-player/bin/uploadConditions.py"
    isolated_env.parameters_path.write_text(json.dumps(params), encoding="utf-8")
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    job_spec = step4.prepare_harvesting_job(ctx, step4.check_files_for_processing(ctx).files_to_process)

    step4.upload_conditions(job_spec)

    upload_script = (job_spec.job_dir / "UPLOAD.sh").read_text(encoding="utf-8")
    assert "\n/opt/ngt/scenario-player/bin/uploadConditions.py NGTCalibEcalPedestals.db\n" in upload_script


def test_upload_retries_independently_of_harvesting(isolated_env, job_runner):
    """The core Step 4 fix: a failed condDB upload can be retried on its own
    without re-running ALCAHARVEST."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    decision = step4.check_files_for_processing(ctx)
    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)
    step4.run_harvesting_job(job_spec)

    job_runner.queue_failure()
    with pytest.raises(shell.JobScriptFailedError):
        step4.upload_conditions(job_spec)
    assert len(job_runner) == 2  # harvest + failed upload attempt

    # Retrying just the upload (no re-harvest call) succeeds without touching HARVESTING.sh again.
    step4.upload_conditions(job_spec)
    assert len(job_runner) == 3
    assert job_runner[2]["script_name"] == "UPLOAD.sh"


def test_beamspot_metadata_has_no_since(isolated_env, job_runner):
    run_dir_path = isolated_env.data_dir / "BeamSpot" / "run398600"
    run_dir_path.mkdir(parents=True)
    ctx = step4.build_run_context("BeamSpot", "398600")
    job_dir = run_dir_path / "alcaPromptJob000"
    job_dir.mkdir(parents=True)
    (job_dir / "beamSpotStep3_job.txt").touch()
    (job_dir / "PromptCalibProdBeamSpotHP.root").write_bytes(b"fake")

    decision = step4.check_files_for_processing(ctx)
    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)

    metadata = json.loads((job_spec.job_dir / "NGTCalibBeamSpot.txt").read_text(encoding="utf-8"))
    assert metadata["since"] is None


def test_no_job_prepared_when_nothing_to_process(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    (run_dir / "runEnd.log").touch()

    decision = step4.check_files_for_processing(ctx)
    job_spec = step4.prepare_harvesting_job(ctx, decision.files_to_process)

    assert job_spec is None
    assert job_runner == []


def test_same_batch_reprepared_reuses_same_job_dir(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")
    decision = step4.check_files_for_processing(ctx)

    job_spec_1 = step4.prepare_harvesting_job(ctx, decision.files_to_process)
    job_spec_2 = step4.prepare_harvesting_job(ctx, decision.files_to_process)

    assert job_spec_1.job_dir == job_spec_2.job_dir


def test_finalize_writes_summary_log_covering_all_harvested_files(isolated_env, job_runner):
    """Two-cycle scenario: a Step3 file is harvested while the run is still
    ongoing (not final), then the run ends with nothing new to add, triggering
    a final cycle covering everything harvested so far."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step4.build_run_context(CALIBRATION, "398600")
    _seed_step3_job_output(run_dir, "alcaPromptJob000")

    decision_1 = step4.check_files_for_processing(ctx)
    assert decision_1.action == "batch"
    job_spec_1 = step4.prepare_harvesting_job(ctx, decision_1.files_to_process)
    step4.run_harvesting_job(job_spec_1)
    step4.upload_conditions(job_spec_1)
    is_final_1 = step4.finalize_cycle(ctx, job_spec_1, decision_1)
    assert is_final_1 is False

    (run_dir / "runEnd.log").touch()
    decision_2 = step4.check_files_for_processing(ctx)
    assert decision_2.action == "final"
    job_spec_2 = step4.prepare_harvesting_job(ctx, decision_2.files_to_process)
    is_final_2 = step4.finalize_cycle(ctx, job_spec_2, decision_2)
    assert is_final_2 is True

    summary = (run_dir / "allStep3FilesProcessed.log").read_text(encoding="utf-8")
    assert str(run_dir / "alcaPromptJob000" / ROOT_NAME) in summary
