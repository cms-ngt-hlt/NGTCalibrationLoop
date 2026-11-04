"""
airflow_automation/airflow_containers/'s image without a container runtime: what goes into it, and
that airflow_demo.sh behaves inside a container.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
AIRFLOW_DEMO = REPO_ROOT / "airflow_automation" / "airflow_demo" / "airflow_demo.sh"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)


def test_image_runs_as_a_numeric_user():
    # A cluster can't check runAsNonRoot on a name: the kubelet refuses to start the container.
    dockerfile = (CONTAINERS / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^USER (\S+)", dockerfile, flags=re.MULTILINE)[-1].isdigit()


def test_image_context_keeps_a_checkouts_own_state_out():
    rules = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split("\n")
    rules = [rule for rule in rules if rule and not rule.startswith("#")]
    assert rules[0] == "*", "an allowlist: anything new in a checkout stays out by default"
    assert "**/local_*_env.sh" in rules and "**/.env" in rules
    allowed = {rule[1:].rstrip("/") for rule in rules if rule.startswith("!")}
    dockerfile = (CONTAINERS / "Dockerfile").read_text(encoding="utf-8")
    copied = {source.rstrip("/") for source in re.findall(r"^COPY --chown=\S+ (\S+) ", dockerfile, flags=re.MULTILINE)}
    assert copied == allowed
    assert all((REPO_ROOT / source).is_dir() for source in copied)


@needs_bash
@pytest.mark.parametrize("command", ["start", "stop", "status", "ui", "logs"])
def test_demo_script_leaves_process_management_to_the_container_runtime(command):
    result = subprocess.run(
        ["bash", str(AIRFLOW_DEMO), command],
        env={"PATH": "/usr/bin:/bin", "NGT_AIRFLOW_IN_CONTAINER": "1", "HOME": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "airflow_containers/README.md" in result.stderr


@needs_bash
def test_demo_script_sets_up_in_a_container_without_venv_or_psql(tmp_path):
    """`setup` is what airflow-init runs: the image is the venv and already has the stub."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    called = tmp_path / "called"
    for tool in ("airflow", "pip", "psql", "createdb", "tmux"):
        path = fake_bin / tool
        path.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{called}"\n', encoding="utf-8")
        path.chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)
    dev_home = tmp_path / "data" / "demo-env"

    result = subprocess.run(
        ["bash", str(AIRFLOW_DEMO), "setup"],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "NGT_AIRFLOW_IN_CONTAINER": "1",
            "NGT_SIM_STUB_INSTALLED": "1",
            "NGT_DEV_HOME": str(dev_home),
            "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "postgresql+psycopg2://airflow@elsewhere/airflow",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert called.read_text(encoding="utf-8").split("\n")[:-1] == ["airflow db migrate"]
    assert (dev_home / "env.sh").is_file()
