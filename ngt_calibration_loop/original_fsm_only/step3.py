"""Step 3's original-FSM-only logic, split out of ngt_calibration_loop/step3.py --
see this package's __init__ for why. Ported from NGTLoopStep3.py's FSM.
"""

from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..step3 import (
    PROCESSED_LOG_NAME,
    RUN_END_LOG_NAME,
    CycleDecision,
    RunContext,
    _path_where_files_appear,
)

MINIMUM_FILES_PER_BATCH = 1


def find_new_run(calibration_name, already_latched_run_numbers):
    """Scan for a run directory not already latched by a processor DAG chain
    (the caller supplies that set, replacing the old in-memory
    setOfRunsProcessed). Returns the earliest new run's number as a string, or
    None."""
    ngt_params = config.load_ngt_parameters()
    path = Path(_path_where_files_appear(ngt_params, calibration_name))
    if not path.exists():
        return None

    current_dirs = {p.name for p in path.iterdir() if p.is_dir()}
    already = {f"run{n}" for n in already_latched_run_numbers}
    new_runs = {p for p in (current_dirs - already) if p.startswith("run")}
    if not new_runs:
        return None

    return sorted(new_runs)[0][3:]


def _run_is_not_complete(working_dir):
    return not (Path(working_dir) / RUN_END_LOG_NAME).exists()


def _still_have_time(start_time, timeout_seconds):
    diff = datetime.now(timezone.utc) - start_time
    return diff.total_seconds() <= timeout_seconds


def load_already_processed(working_dir):
    log_path = Path(working_dir) / PROCESSED_LOG_NAME
    if not log_path.exists():
        return set()
    return {line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _available_files(ctx: RunContext):
    conf = ctx.calib_config["step_3_config"]
    suffix_control = conf["step_2_witness_suffix"]
    root_suffix = conf["step_2_root_suffix"]

    control_files = {str(p) for p in Path(ctx.working_dir).glob(f"run*{suffix_control}")}
    changed = {
        (s[: -len(suffix_control)] + root_suffix if s.endswith(suffix_control) else s)
        for s in control_files
    }
    return {Path(s) for s in changed}


def check_files_for_processing(ctx: RunContext):
    """Mirrors CheckFilesForProcessing + the ContinueAfterCheckFiles transition
    conditions, in the same precedence order as the original transition list:
    enough files waiting takes priority even over an expired timeout (unlike
    Step 2, where an expired timeout wins outright)."""
    already_processed = {Path(p) for p in load_already_processed(ctx.working_dir)}
    available = _available_files(ctx)
    files_to_process = available - already_processed

    enough_files = len(files_to_process) >= MINIMUM_FILES_PER_BATCH

    if files_to_process and enough_files:
        return CycleDecision(action="batch", files_to_process=files_to_process)

    if _run_is_not_complete(ctx.working_dir) and _still_have_time(ctx.start_time, ctx.timeout_seconds):
        return CycleDecision(action="wait", files_to_process=files_to_process)

    return CycleDecision(action="final", files_to_process=files_to_process)
