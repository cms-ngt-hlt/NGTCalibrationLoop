"""Tests for ngt_calibration_loop.step3: building a run's context (start time,
per-calibration timeout), and the ALCAPROMPT job prep/launch/finalize cycle.

The FSM-only run discovery and witness-file batching decisions are covered in
test_original_fsm_only.py. Here each cycle's decision is built the way the
AssetWatcher design does (airflow_dags/_process_tasks.py): process exactly the
Step 2 output file(s) the task was triggered for, with no directory scan.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ngt_calibration_loop import shell, step3

CALIBRATION = "EcalPedestals"
ROOT_SUFFIX = "ecalPedsStep2.root"


def _patch_calib_yaml(isolated_env, calibration, **step_3_config_overrides):
    """Merge `step_3_config_overrides` into the copied calibrationYAML/
    {calibration}.yaml -- e.g. timeoutSeconds, same as scenario-player/seed.py's
    cmd_setup patches it for the live-test setup (see step3.py's
    DEFAULT_TIMEOUT_SECONDS)."""
    path = isolated_env.calib_yaml_dir / f"{calibration}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["step_3_config"].update(step_3_config_overrides)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _run_dir(isolated_env, run_number):
    return isolated_env.data_dir / CALIBRATION / f"run{run_number}"


def _seed_step2_root_file(run_dir, basename):
    """Create a Step 2 output root file the way ngt_calibration_loop.step2's
    run_express_job would have, and return its path."""
    root_file = run_dir / f"{basename}_{ROOT_SUFFIX}"
    root_file.write_bytes(b"fake root file")
    return root_file


def _batch_of(*root_files):
    """The decision the AssetWatcher design hands Step 3 for a triggering Step 2
    output (see airflow_dags/_process_tasks.py)."""
    return step3.CycleDecision(action="batch", files_to_process=set(root_files))


# --- Run context ---------------------------------------------------------------------


def test_build_run_context_points_at_the_run_directory(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    ctx = step3.build_run_context(CALIBRATION, "398600")

    assert Path(ctx.working_dir) == run_dir


def test_reads_run_start_time_from_runstart_log(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    start_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
    (run_dir / "runStart.log").write_text(start_time.isoformat(), encoding="utf-8")

    ctx = step3.build_run_context(CALIBRATION, "398600")

    assert ctx.start_time == start_time


def test_defaults_start_time_to_now_without_runstart_log(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    before = datetime.now(timezone.utc)

    ctx = step3.build_run_context(CALIBRATION, "398600")

    assert before <= ctx.start_time <= datetime.now(timezone.utc)
    assert (run_dir / "runStart.log").exists()  # self-healed for later cycles


def test_timeout_seconds_is_configurable_per_calibration(isolated_env):
    """See test_step2_lib.py's equivalent test / step3.py's
    DEFAULT_TIMEOUT_SECONDS for why the live-test setup needs this. The
    decision it drives is covered in test_original_fsm_only.py."""
    _patch_calib_yaml(isolated_env, CALIBRATION, timeoutSeconds=5)

    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")

    assert ctx.timeout_seconds == 5


# --- ALCAPROMPT job prep / launch / finalize ------------------------------------------


def test_input_file_missing_at_prepare_time_is_dropped(isolated_env):
    """A Step 2 output can be announced (its witness file written) before its
    root file is fully flushed to disk; prepare_alca_prompt_job re-checks
    existence and drops anything missing."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    never_created = run_dir / f"run398600_LS0051To0051_{ROOT_SUFFIX}"

    job_spec = step3.prepare_alca_prompt_job(ctx, {never_created})

    assert job_spec is None


def test_prepare_and_launch_alcaprompt_job_full_cycle(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    decision = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0051To0051"))

    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    assert job_spec is not None
    assert job_spec.job_dir.parent == run_dir
    assert job_spec.job_dir.name.startswith("alcaPromptJob_")
    script = (job_spec.job_dir / "ALCAOUTPUT.sh").read_text(encoding="utf-8")
    assert "cmsDriver.py expressStep3" in script
    assert "ALCA:PromptCalibProdEcalPedestals" in script

    step3.run_alca_prompt_job(job_spec)
    assert len(job_runner) == 1
    assert job_runner[0]["script_name"] == "ALCAOUTPUT.sh"
    assert job_runner[0]["cwd"] == str(job_spec.job_dir)

    is_final = step3.finalize_cycle(ctx, job_spec, decision)
    assert is_final is False  # not final -> keep watching this run


def test_same_batch_reprepared_reuses_same_job_dir(isolated_env, job_runner):
    """Deterministic (content-hashed) job dir naming: re-running
    prepare_alca_prompt_job for the identical input set -- e.g. an Airflow
    task retry -- reproduces the same directory instead of minting a new one,
    so a retry doesn't double-submit."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    decision = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0051To0051"))

    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    job_spec_2 = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)

    assert job_spec_1.job_dir == job_spec_2.job_dir


def test_second_alcaprompt_job_gets_a_different_job_dir(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    decision_1 = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0051To0051"))
    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision_1.files_to_process)
    step3.run_alca_prompt_job(job_spec_1)
    step3.finalize_cycle(ctx, job_spec_1, decision_1)

    decision_2 = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0052To0052"))
    job_spec_2 = step3.prepare_alca_prompt_job(ctx, decision_2.files_to_process)

    assert job_spec_2.job_dir != job_spec_1.job_dir


def test_no_job_prepared_when_nothing_to_process(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    decision = step3.CycleDecision(action="final", files_to_process=set())  # run ended, no Step 2 output at all

    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)

    assert job_spec is None
    assert job_runner == []


def test_finalize_writes_summary_log_and_survives_two_cycles(isolated_env, job_runner):
    """Two-cycle scenario mirroring a real run: a Step2 file is processed
    while the run is still ongoing (not final), then the run ends with
    nothing new left to process, which triggers a final cycle. The summary
    log accumulates across both."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")

    # Cycle 1: file arrives while the run is still ongoing -> processed normally.
    decision_1 = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0051To0051"))
    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision_1.files_to_process)
    step3.run_alca_prompt_job(job_spec_1)
    is_final_1 = step3.finalize_cycle(ctx, job_spec_1, decision_1)
    assert is_final_1 is False

    # Cycle 2: the run ends with no further Step2 files -> final cycle.
    (run_dir / "runEnd.log").touch()
    decision_2 = step3.CycleDecision(action="final", files_to_process=set())
    job_spec_2 = step3.prepare_alca_prompt_job(ctx, decision_2.files_to_process)
    assert job_spec_2 is None  # nothing new this cycle
    is_final_2 = step3.finalize_cycle(ctx, job_spec_2, decision_2)
    assert is_final_2 is True

    summary = (run_dir / "allStep2FilesProcessed.log").read_text(encoding="utf-8")
    assert f"run398600_LS0051To0051_{ROOT_SUFFIX}" in summary


def test_run_alca_prompt_job_raises_on_failure_for_airflow_retry(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    decision = _batch_of(_seed_step2_root_file(run_dir, "run398600_LS0051To0051"))
    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)

    job_runner.queue_failure()
    with pytest.raises(shell.JobScriptFailedError):
        step3.run_alca_prompt_job(job_spec)
