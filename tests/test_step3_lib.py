"""Tests for ngt_calibration_loop.step3: discovering new run directories,
incrementally batching Step 2 output files via their witness (*_job.txt)
files, the run-not-complete/still-have-time escape hatches, and the
ALCAPROMPT job prep/launch/finalize cycle.

1:1 behavioral port of the old tests/test_step3.py.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ngt_calibration_loop import shell, step3

CALIBRATION = "EcalPedestals"
WITNESS_SUFFIX = "ecalPedsStep2_job.txt"
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


def _seed_step2_output(run_dir, basename, create_root_file=True):
    """Create a Step 2 witness file (and, by default, its matching root file)
    the way ngt_calibration_loop.step2's prepare_express_job/run_express_job
    would have."""
    (run_dir / f"{basename}_{WITNESS_SUFFIX}").touch()
    if create_root_file:
        (run_dir / f"{basename}_{ROOT_SUFFIX}").write_bytes(b"fake root file")


# --- Run discovery ---------------------------------------------------------------


def test_no_calibration_dir_yet_returns_none(isolated_env):
    assert step3.find_new_run(CALIBRATION, already_latched_run_numbers=set()) is None


def test_finds_new_run_directory(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    run_number = step3.find_new_run(CALIBRATION, already_latched_run_numbers=set())

    assert run_number == "398600"
    ctx = step3.build_run_context(CALIBRATION, run_number)
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


def test_ignores_already_latched_runs(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    assert step3.find_new_run(CALIBRATION, already_latched_run_numbers={"398600"}) is None


def test_picks_earliest_of_several_new_runs(isolated_env):
    _run_dir(isolated_env, 398700).mkdir(parents=True)
    _run_dir(isolated_env, 398600).mkdir(parents=True)

    assert step3.find_new_run(CALIBRATION, already_latched_run_numbers=set()) == "398600"


# --- Step 2 file discovery (witness-file driven) -------------------------------------


def test_check_files_for_processing_finds_new_witness_files(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    decision = step3.check_files_for_processing(ctx)

    assert len(decision.files_to_process) == 1
    assert decision.action == "batch"
    (only_file,) = decision.files_to_process
    assert only_file.name == f"run398600_LS0051To0051_{ROOT_SUFFIX}"


def test_no_new_files_run_ongoing_time_left_stays_waiting(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")

    decision = step3.check_files_for_processing(ctx)

    assert decision.action == "wait"


def test_no_new_files_run_ended_moves_to_final(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    (run_dir / "runEnd.log").touch()

    decision = step3.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_no_new_files_timeout_expired_moves_to_final_even_if_run_ongoing(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    ctx.start_time = datetime.now(timezone.utc) - timedelta(hours=10)  # > TIMEOUT_SECONDS (9h)

    decision = step3.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_timeout_seconds_is_configurable_per_calibration(isolated_env):
    """See test_step2_lib.py's equivalent test / step3.py's
    DEFAULT_TIMEOUT_SECONDS for why the live-test setup needs this."""
    _patch_calib_yaml(isolated_env, CALIBRATION, timeoutSeconds=5)

    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    assert ctx.timeout_seconds == 5

    # 1 minute old: past the tiny 5s override, but well inside the 9h
    # production default -- proves the override actually governs this.
    ctx.start_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    decision = step3.check_files_for_processing(ctx)
    assert decision.action == "final"


def test_enough_files_wins_over_expired_timeout(isolated_env):
    """Distinguishing behavior vs. Step 2: Step 3 checks "enough files waiting"
    before checking the timeout, so an expired timeout does NOT force a final
    batch if there's a full batch ready to go."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    ctx.start_time = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    decision = step3.check_files_for_processing(ctx)

    assert decision.action == "batch"


def test_witness_file_without_matching_root_file_is_dropped_at_prepare_time(isolated_env):
    """A witness file can appear before its root file is fully flushed to
    disk; prepare_alca_prompt_job re-checks existence and drops anything
    missing."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051", create_root_file=False)

    decision = step3.check_files_for_processing(ctx)
    assert len(decision.files_to_process) == 1  # witness file was seen...

    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    assert job_spec is None  # ...but its root file never existed


# --- ALCAPROMPT job prep / launch / finalize ------------------------------------------


def test_prepare_and_launch_alcaprompt_job_full_cycle(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    decision = step3.check_files_for_processing(ctx)
    assert decision.action == "batch"

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
    _seed_step2_output(run_dir, "run398600_LS0051To0051")
    decision = step3.check_files_for_processing(ctx)

    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    job_spec_2 = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)

    assert job_spec_1.job_dir == job_spec_2.job_dir


def test_second_alcaprompt_job_gets_a_different_job_dir(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    decision = step3.check_files_for_processing(ctx)
    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    step3.run_alca_prompt_job(job_spec_1)
    step3.finalize_cycle(ctx, job_spec_1, decision)

    _seed_step2_output(run_dir, "run398600_LS0052To0052")
    decision_2 = step3.check_files_for_processing(ctx)
    job_spec_2 = step3.prepare_alca_prompt_job(ctx, decision_2.files_to_process)

    assert job_spec_2.job_dir != job_spec_1.job_dir


def test_no_job_prepared_when_nothing_to_process(isolated_env, job_runner):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    (run_dir / "runEnd.log").touch()  # run ends with no Step 2 output at all

    decision = step3.check_files_for_processing(ctx)
    assert decision.action == "final"

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
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    # Cycle 1: file arrives while the run is still ongoing -> processed normally.
    decision_1 = step3.check_files_for_processing(ctx)
    assert decision_1.action == "batch"
    job_spec_1 = step3.prepare_alca_prompt_job(ctx, decision_1.files_to_process)
    step3.run_alca_prompt_job(job_spec_1)
    is_final_1 = step3.finalize_cycle(ctx, job_spec_1, decision_1)
    assert is_final_1 is False

    # Cycle 2: the run ends with no further Step2 files -> final cycle.
    (run_dir / "runEnd.log").touch()
    decision_2 = step3.check_files_for_processing(ctx)
    assert decision_2.action == "final"
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
    _seed_step2_output(run_dir, "run398600_LS0051To0051")
    decision = step3.check_files_for_processing(ctx)
    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)

    job_runner.queue_failure()
    with pytest.raises(shell.JobScriptFailedError):
        step3.run_alca_prompt_job(job_spec)
