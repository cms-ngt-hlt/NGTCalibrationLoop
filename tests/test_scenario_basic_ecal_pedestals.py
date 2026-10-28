"""
End-to-end "scenario-test": plays scenario-player/scenarios/basic_ecal_pedestals.yaml through
scenario-player/live_demo.sh (real NGTLoopStep2/3/4.py processes in tmux against the fake
OMS/EOS/CMSSW toolchain) and checks what the loops left behind in a scratch $NGT_DEV_HOME.

Slow (about a minute) and needs bash + tmux + the repo-root .venv, so it is deselected from the
default `pytest` run (see pytest.ini) -- run it with `pytest -m scenario` (CI: scenario-test).
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DEMO = REPO_ROOT / "scenario-player" / "live_demo.sh"
SCENARIO = REPO_ROOT / "scenario-player" / "scenarios" / "basic_ecal_pedestals.yaml"
CALIBRATION = "EcalPedestals"
SPEED = 2
PIPELINE_TIMEOUT_SECONDS = 240  # after playback ends: Step 2 -> 3 -> 4 still have to finish

pytestmark = [
    pytest.mark.scenario,
    pytest.mark.skipif(
        shutil.which("bash") is None or shutil.which("tmux") is None,
        reason="needs bash and tmux",
    ),
]


def _lines(path):
    """Whitespace-separated entries of a text file, or [] if it doesn't exist (yet)."""
    return path.read_text().split() if path.exists() else []


def _wait_until(condition, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(2)
    return condition()


@pytest.fixture
def demo_home(tmp_path):
    """Scratch $NGT_DEV_HOME, with the demo's tmux sessions torn down afterwards."""
    env = dict(os.environ, NGT_DEV_HOME=str(tmp_path / "demo-env"), NGT_LOOP_SLEEP_SECONDS="2")

    def demo(*args):
        return subprocess.run(
            ["bash", str(LIVE_DEMO), *args], env=env, capture_output=True, text=True, check=False
        )

    assert "(no NGTDemo* tmux sessions" in demo("status").stdout, (
        "NGTDemo* tmux sessions are already running -- stop them first (live_demo.sh stop-all)"
    )
    try:
        yield Path(env["NGT_DEV_HOME"]), demo
    finally:
        demo("stop-all", CALIBRATION)


def test_basic_ecal_pedestals_scenario(demo_home):
    home, demo = demo_home
    spec = yaml.safe_load(SCENARIO.read_text())["runs"][0]
    run = spec["run"]
    ls_numbers = [ls["ls"] for ls in spec["lumisections"]]
    run_dir = home / "data" / CALIBRATION / f"run{run}"

    played = demo("play", str(SCENARIO), "--speed", str(SPEED))
    assert played.returncode == 0, played.stdout + played.stderr

    # Playback is over; give the loops time to finish. Step 4 writes allStep3FilesProcessed.log once,
    # after launching its last harvest job -- detached, so it may still be running; wait for it too.
    _wait_until(
        lambda: (run_dir / "allStep3FilesProcessed.log").exists()
        and subprocess.run(["pgrep", "-f", "HARVESTING.sh"], capture_output=True).returncode != 0,
        PIPELINE_TIMEOUT_SECONDS,
    )

    # The scenario player dropped one fake EOS file per lumisection...
    eos_files = sorted((home / "eos" / CALIBRATION).rglob(f"run{run}_ls*.root"))
    assert [f.name for f in eos_files] == [f"run{run}_ls{ls:04d}.root" for ls in ls_numbers]

    # ...Step 2 picked all of them up...
    assert {Path(p).name for p in _lines(run_dir / "allLSProcessed.log")} == {f.name for f in eos_files}

    # ...and each stage's outputs (1-3 files, however Step 2/3 chose to batch them) were consumed by
    # the next. Step 2's outputs are found by their job scripts; Step 3's by their job directories.
    step2_outputs = {p.with_suffix(".root").name for p in run_dir.glob(f"run{run}_LS*_ecalPedsStep2.py")}
    assert step2_outputs, "no Step 2 job was created"
    assert step2_outputs <= {Path(p).name for p in _lines(run_dir / "allStep2FilesProcessed.log")}

    step3_outputs = {f"{p.parent.name}/{p.name}" for p in run_dir.glob("alcaPromptJob*/*.root")}
    assert step3_outputs, "no Step 3 job produced an output"
    assert step3_outputs <= {"/".join(Path(p).parts[-2:]) for p in _lines(run_dir / "allStep3FilesProcessed.log")}

    # Step 4 harvested at least once...
    assert list(run_dir.glob("harvestJob*/*.db")), "no harvestJob directory contains a .db file"

    # ...and nothing along the way wrote an error.
    error_logs = [*(home / "data").rglob("*stderr.log"), *(home / "logs").rglob("*ERROR.log")]
    assert error_logs, "found no stderr/ERROR logs to check"
    assert {str(f.relative_to(home)): f.read_text() for f in error_logs if f.stat().st_size} == {}
