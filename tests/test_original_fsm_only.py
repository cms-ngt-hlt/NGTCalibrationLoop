"""Tests for ngt_calibration_loop.original_fsm_only: the run-directory scan
(Steps 3 and 4) and Step 3's witness-file-driven batch/wait/final decision that
only the original FSM used -- no Airflow design calls them (see the package
docstring).

Moved here from test_step3_lib.py / test_step4_lib.py together with the
functions they cover; the rest of those files exercise what the DAGs still call.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ngt_calibration_loop import step3, step4
from ngt_calibration_loop.original_fsm_only import step3 as fsm_step3
from ngt_calibration_loop.original_fsm_only import step4 as fsm_step4

CALIBRATION = "EcalPedestals"
WITNESS_SUFFIX = "ecalPedsStep2_job.txt"
ROOT_SUFFIX = "ecalPedsStep2.root"


def _patch_step_3_config(isolated_env, calibration, **overrides):
    """Merge `overrides` into the copied calibrationYAML/{calibration}.yaml's
    step_3_config -- e.g. timeoutSeconds, same as scenario-player/seed.py's
    cmd_setup patches it for the live-test setup."""
    path = isolated_env.calib_yaml_dir / f"{calibration}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["step_3_config"].update(overrides)
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


# --- Step 3 run discovery --------------------------------------------------------------


def test_step3_no_calibration_dir_yet_returns_none(isolated_env):
    assert fsm_step3.find_new_run(CALIBRATION, already_latched_run_numbers=set()) is None


def test_step3_finds_new_run_directory(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    run_number = fsm_step3.find_new_run(CALIBRATION, already_latched_run_numbers=set())

    assert run_number == "398600"
    ctx = step3.build_run_context(CALIBRATION, run_number)
    assert Path(ctx.working_dir) == run_dir


def test_step3_ignores_already_latched_runs(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    assert fsm_step3.find_new_run(CALIBRATION, already_latched_run_numbers={"398600"}) is None


def test_step3_picks_earliest_of_several_new_runs(isolated_env):
    _run_dir(isolated_env, 398700).mkdir(parents=True)
    _run_dir(isolated_env, 398600).mkdir(parents=True)

    assert fsm_step3.find_new_run(CALIBRATION, already_latched_run_numbers=set()) == "398600"


# --- Step 4 run discovery --------------------------------------------------------------


def test_step4_no_calibration_dir_yet_returns_none(isolated_env):
    assert fsm_step4.find_new_run(CALIBRATION, already_latched_run_numbers=set()) is None


def test_step4_finds_new_run_directory(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    run_number = fsm_step4.find_new_run(CALIBRATION, already_latched_run_numbers=set())

    assert run_number == "398600"
    ctx = step4.build_run_context(CALIBRATION, run_number)
    assert Path(ctx.working_dir) == run_dir


def test_step4_ignores_already_latched_runs(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)

    assert fsm_step4.find_new_run(CALIBRATION, already_latched_run_numbers={"398600"}) is None


# --- Step 3 file discovery (witness-file driven) ---------------------------------------


def test_check_files_for_processing_finds_new_witness_files(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051")

    decision = fsm_step3.check_files_for_processing(ctx)

    assert len(decision.files_to_process) == 1
    assert decision.action == "batch"
    (only_file,) = decision.files_to_process
    assert only_file.name == f"run398600_LS0051To0051_{ROOT_SUFFIX}"


def test_no_new_files_run_ongoing_time_left_stays_waiting(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")

    decision = fsm_step3.check_files_for_processing(ctx)

    assert decision.action == "wait"


def test_no_new_files_run_ended_moves_to_final(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    (run_dir / "runEnd.log").touch()

    decision = fsm_step3.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_no_new_files_timeout_expired_moves_to_final_even_if_run_ongoing(isolated_env):
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    ctx.start_time = datetime.now(timezone.utc) - timedelta(hours=10)  # > TIMEOUT_SECONDS (9h)

    decision = fsm_step3.check_files_for_processing(ctx)

    assert decision.action == "final"


def test_timeout_override_governs_the_final_decision(isolated_env):
    """The decision-side half of test_step3_lib.py's
    test_timeout_seconds_is_configurable_per_calibration."""
    _patch_step_3_config(isolated_env, CALIBRATION, timeoutSeconds=5)

    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")

    # 1 minute old: past the tiny 5s override, but well inside the 9h
    # production default -- proves the override actually governs this.
    ctx.start_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    decision = fsm_step3.check_files_for_processing(ctx)
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

    decision = fsm_step3.check_files_for_processing(ctx)

    assert decision.action == "batch"


def test_witness_file_without_matching_root_file_is_dropped_at_prepare_time(isolated_env):
    """A witness file can appear before its root file is fully flushed to
    disk; prepare_alca_prompt_job re-checks existence and drops anything
    missing."""
    run_dir = _run_dir(isolated_env, 398600)
    run_dir.mkdir(parents=True)
    ctx = step3.build_run_context(CALIBRATION, "398600")
    _seed_step2_output(run_dir, "run398600_LS0051To0051", create_root_file=False)

    decision = fsm_step3.check_files_for_processing(ctx)
    assert len(decision.files_to_process) == 1  # witness file was seen...

    job_spec = step3.prepare_alca_prompt_job(ctx, decision.files_to_process)
    assert job_spec is None  # ...but its root file never existed
