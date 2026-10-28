"""
End-to-end "scenario-tests": play scenario-player/scenarios/*.yaml through
scenario-player/live_demo.sh (real NGTLoopStep2/3/4.py processes in tmux against the fake
OMS/EOS/CMSSW toolchain) and check what the loops left behind in a scratch $NGT_DEV_HOME.

Slow (a couple of minutes each) and need bash + tmux + the repo-root .venv, so they are deselected
from the default `pytest` run (see pytest.ini) -- run them with `pytest -m scenario` (CI: scenario-test).
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

import fake_conddb  # scenario-player/fake_conddb.py, on sys.path via tests/conftest.py
import faults

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DEMO = REPO_ROOT / "scenario-player" / "live_demo.sh"
SCENARIOS = REPO_ROOT / "scenario-player" / "scenarios"
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


def _error_logs(home):
    """The non-empty stderr/ERROR logs under $NGT_DEV_HOME, {relative path: contents}."""
    error_logs = [*(home / "data").rglob("*stderr.log"), *(home / "logs").rglob("*ERROR.log")]
    assert error_logs, "found no stderr/ERROR logs to check"
    return {str(f.relative_to(home)): f.read_text() for f in error_logs if f.stat().st_size}


@pytest.fixture
def demo_home(tmp_path):
    """Scratch $NGT_DEV_HOME, with the demo's tmux sessions torn down afterwards. Its sessions
    are named after it, so they stay apart from a demo of the same checkout that may be running."""
    env = dict(os.environ, NGT_DEV_HOME=str(tmp_path / "demo-env"), NGT_LOOP_SLEEP_SECONDS="2")

    def demo(*args):
        return subprocess.run(
            ["bash", str(LIVE_DEMO), *args], env=env, capture_output=True, text=True, check=False
        )

    assert "(no NGTDemo_" in demo("status").stdout, "this test's tmux sessions are already running"
    try:
        yield Path(env["NGT_DEV_HOME"]), demo
    finally:
        demo("stop-all", CALIBRATION)


def _play(demo_home, scenario):
    """Play `scenario` (one run, CALIBRATION only) and wait for the loops to finish it.
    Returns the scenario's run spec and the run's working directory."""
    home, demo = demo_home
    spec = yaml.safe_load(scenario.read_text())
    run_dir = home / "data" / CALIBRATION / f"run{spec['runs'][0]['run']}"

    # The player checks the scenario's expect: block once the uploads settle, and exits 1 if it fails.
    played = demo("play", str(scenario), "--speed", str(SPEED))
    assert played.returncode == 0, played.stdout + played.stderr

    # Playback is over; give the loops time to finish. Step 4 writes allStep3FilesProcessed.log once,
    # after launching its last harvest job -- detached, so it may still be running; wait for it too.
    _wait_until(
        lambda: (run_dir / "allStep3FilesProcessed.log").exists()
        and subprocess.run(["pgrep", "-f", "HARVESTING.sh"], capture_output=True).returncode != 0,
        PIPELINE_TIMEOUT_SECONDS,
    )
    return spec, run_dir


def test_basic_ecal_pedestals_scenario(demo_home):
    home, _ = demo_home
    spec, run_dir = _play(demo_home, SCENARIOS / "basic_ecal_pedestals.yaml")
    run = spec["runs"][0]["run"]
    ls_numbers = [ls["ls"] for ls in spec["runs"][0]["lumisections"]]

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

    # ...and every upload is this run's, the last covering every lumisection (how many come before it
    # depends on timing).
    rows = fake_conddb.read_ledger(str(home / "conddb" / fake_conddb.LEDGER_NAME))
    assert rows, "nothing reached the dummy conditions DB"
    assert {(row["calibration"], row["run"]) for row in rows} == {(CALIBRATION, run)}
    assert rows[-1]["lumisections"] == ls_numbers

    # ...and nothing along the way wrote an error.
    assert _error_logs(home) == {}


def test_fault_injection_demo_scenario(demo_home):
    """OMS fails twice and the first upload fails -- and the final upload still has every lumisection."""
    home, _ = demo_home
    spec, run_dir = _play(demo_home, SCENARIOS / "fault_injection_demo.yaml")
    run = spec["runs"][0]["run"]
    ls_numbers = [ls["ls"] for ls in spec["runs"][0]["lumisections"]]

    # Every lumisection is in a successful Step 2 job (each leaves a witness file named after its LS range)...
    witnesses = list(run_dir.glob(f"run{run}_LS*_ecalPedsStep2_job.txt"))
    step2_ls = set()
    for witness in witnesses:
        first, last = map(int, re.search(r"_LS(\d+)To(\d+)_", witness.name).groups())
        step2_ls |= set(range(first, last + 1))
    assert step2_ls == set(ls_numbers)

    # ...every Step 2 output went into a Step 3 job, and every Step 3 job produced its output...
    step2_outputs = {w.name.replace("_job.txt", ".root") for w in witnesses}
    assert step2_outputs <= {Path(p).name for p in _lines(run_dir / "allStep2FilesProcessed.log")}
    step3_jobs = sorted(run_dir.glob("alcaPromptJob*"))
    assert step3_jobs and all(list(job.glob("*.root")) for job in step3_jobs)

    # ...and the last harvest took every Step 3 output and uploaded it.
    harvests = sorted(run_dir.glob("harvestJob*"))
    filein = re.search(r"--filein (\S+)", (harvests[-1] / "HARVESTING.sh").read_text()).group(1)
    assert {Path(f[len("file:"):]) for f in filein.split(",")} == set(run_dir.glob("alcaPromptJob*/*.root"))
    assert "(FAKE) uploadConditions.py: would upload" in (harvests[-1] / "stdout.log").read_text()

    # The only errors are the injected ones -- the first upload and Step 2's OMS queries -- and
    # every armed fault was used up.
    oms, upload = (
        faults.fault_spec_to_entry(faults.parse_fault_spec({k: v for k, v in f.items() if k != "at"}))
        for f in spec["faults"]
    )
    upload_errors = str((harvests[0] / "stderr.log").relative_to(home))
    step2_errors = f"logs/{CALIBRATION}/NGTLoopStep2_ERROR.log"
    errors = _error_logs(home)
    assert errors.keys() == {upload_errors, step2_errors}
    assert errors[upload_errors] == faults.describe("upload_conditions", upload) + "\n"
    assert faults.describe("oms", oms) in errors[step2_errors]
    assert all(_lines(f) == ["[]"] for f in (home / "faults").glob("*.json"))
