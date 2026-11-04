"""
End-to-end runs of the container deployments in airflow_automation/airflow_containers/: a scenario
played against the separate-containers Compose stack and against the emulated one-pod deployment,
each running as an arbitrary non-root UID. Slow (minutes), needs Docker with
Compose v2 and pulls images, so it's deselected from the default `pytest` run (see pytest.ini) -- run
it with `pytest -m container`. Uses its own Compose projects and ports, next to anything running.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
COMPOSE_SH = CONTAINERS / "compose" / "airflow_compose.sh"
ONEPOD_DIR = CONTAINERS / "onepod"
SCENARIO = "scenario-player/scenarios/basic_ecal_pedestals.yaml"
IMAGE = "ngt-airflow:pytest"

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker"),
]


def _run(args, env, timeout=1200, cwd=REPO_ROOT):
    return subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)


def _wait_healthy(port, timeout=240):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/v2/monitor/health", timeout=3) as response:
                last = json.load(response)
            if all(last[c]["status"] == "healthy" for c in ("metadatabase", "scheduler", "triggerer", "dag_processor")):
                return
        except OSError:
            pass
        time.sleep(2)
    pytest.fail(f"Airflow never became healthy: {last}")


def test_scenario_passes_on_the_separate_containers_stack(tmp_path):
    env = dict(
        os.environ,
        NGT_SKIP_LOCAL_ENV="1",
        COMPOSE_PROJECT_NAME="ngt_airflow_pytest",
        NGT_COMPOSE_PORT="18291",
        NGT_AIRFLOW_IMAGE=IMAGE,
        NGT_COMPOSE_ENV_FILE=str(tmp_path / "secrets.env"),
    )
    try:
        built = _run(["bash", str(COMPOSE_SH), "build"], env)
        assert built.returncode == 0, built.stdout + built.stderr

        # The player checks the scenario's expect: block itself, and exits 1 if it fails.
        played = _run(["bash", str(COMPOSE_SH), "play", SCENARIO], env)
        assert played.returncode == 0, played.stdout + played.stderr
        assert "expectations: PASSED" in played.stdout

        # A scenario replayed after a reset works again, which needs all the state gone.
        reset = _run(["bash", str(COMPOSE_SH), "reset-scenario"], env)
        assert reset.returncode == 0, reset.stdout + reset.stderr
        listed = _run(["bash", str(COMPOSE_SH), "list-runs"], env)
        assert "no runs seeded" in listed.stdout, listed.stdout + listed.stderr
    finally:
        _run(["bash", str(COMPOSE_SH), "down"], env)


def test_scenario_passes_on_the_one_pod_emulation():
    env = dict(
        os.environ,
        COMPOSE_PROJECT_NAME="ngt_onepod_pytest",
        NGT_ONEPOD_PORT="18292",
        NGT_AIRFLOW_IMAGE=IMAGE,
    )
    compose = ["docker", "compose"]
    try:
        up = _run([*compose, "up", "-d", "--build"], env, cwd=ONEPOD_DIR)
        assert up.returncode == 0, up.stdout + up.stderr
        _wait_healthy(18292)

        # What the tutorial has you run, with `kubectl exec deploy/ngt-airflow -c airflow --`.
        played = _run([*compose, "exec", "-T", "airflow", "airflow_demo.sh", "play", SCENARIO], env, cwd=ONEPOD_DIR)
        assert played.returncode == 0, played.stdout + played.stderr
        assert "expectations: PASSED" in played.stdout

        with pytest.raises(urllib.error.HTTPError) as anonymous:
            urllib.request.urlopen("http://localhost:18292/api/v2/dags", timeout=5)
        assert anonymous.value.code == 401
    finally:
        _run([*compose, "down", "-v"], env, cwd=ONEPOD_DIR)
