"""
Tests that the fake cmsRun and uploadConditions.py (scenario-player/bin/) act on a fault armed for the
job they run in -- printing faults.describe()'s line and exiting with its code -- and leave one scoped
elsewhere armed. They run the real scripts from a job directory like the loops' own; which directory
means which scope is faults.scope_from_cwd's, tested in test_faults.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs fcntl")

BIN = Path(__file__).resolve().parents[2] / "scenario-player" / "bin"


def _run_fake(tmp_path, fake, target, entry):
    """Arm `entry` for `target`, run `fake` in a Step 4 job dir; return its result and what is left armed."""
    faults_dir = tmp_path / "faults"
    faults_dir.mkdir()
    armed = faults_dir / f"{target}.json"
    entry = {"mode": "exit_code", "exit_code": 139, "times_remaining": 1, **entry}
    armed.write_text(json.dumps([entry]), encoding="utf-8")
    job_dir = tmp_path / "data" / "EcalPedestals" / "run398600" / "harvestJob000"
    job_dir.mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(BIN / fake), "no_such_file"],
        cwd=str(job_dir),
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, NGT_FAULTS_DIR=str(faults_dir)),
    )
    return result, json.loads(armed.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "fake, target", [("cmsRun", "cmsrun"), ("uploadConditions.py", "upload_conditions")]
)
def test_a_fault_armed_for_the_job_fires_once(tmp_path, fake, target):
    result, left = _run_fake(tmp_path, fake, target, {"calibration": "EcalPedestals", "run": 398600})

    assert result.returncode == 139
    assert (
        f"(FAKE) injected {target} fault: mode=exit_code exit_code=139 calibration=EcalPedestals run=398600"
        in result.stderr
    )
    assert left == []


def test_a_fault_scoped_to_another_step_is_left_armed(tmp_path):
    result, left = _run_fake(tmp_path, "cmsRun", "cmsrun", {"step": "step2"})

    assert result.returncode == 1  # no fault: the fake just can't find its python config
    assert "injected" not in result.stderr
    assert len(left) == 1
