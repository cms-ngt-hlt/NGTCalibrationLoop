"""Step 4's original-FSM-only logic, split out of ngt_calibration_loop/step4.py --
see this package's __init__ for why. Ported from NGTLoopStep4.py's FSM.

Only `find_new_run` moved: `check_files_for_processing` (and the
`load_already_processed` it calls) is still used by the AssetWatcher design's harvest
and upload tasks, so it stays in step4.py.
"""

from pathlib import Path

from .. import config
from ..step4 import _path_where_files_appear


def find_new_run(calibration_name, already_latched_run_numbers):
    """Same run-directory-scan/dedup pattern as step3.find_new_run (this
    package's step3 module)."""
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
