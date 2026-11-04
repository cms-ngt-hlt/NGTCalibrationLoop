"""
Tests what sim_env.sh's sim_setup writes into $NGT_DEV_HOME/env.sh -- the one file every live
adapter (scenario-player/live_demo.sh's loops among them) sources for the fake environment. Like
tests/scenario-player/test_fake_toolchain_isolation.py these spawn a real bash, since what matters is what a
shell sees after sourcing it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX bash")

REPO_ROOT = Path(__file__).resolve().parents[2]
SIM_ENV = REPO_ROOT / "scenario-player" / "sim_env.sh"


@pytest.fixture
def env_sh(tmp_path):
    """Run sim_setup into a scratch $NGT_DEV_HOME, with a no-op `pip` first on $PATH so its
    `pip install -e tests/stubs` step leaves the test's own venv alone."""
    home = tmp_path / "demo-env"
    fake_bin = tmp_path / "fake-pip"
    fake_bin.mkdir()
    pip = fake_bin / "pip"
    pip.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pip.chmod(0o755)
    script = (
        f'export NGT_DEV_HOME="{home}" VIRTUAL_ENV="{tmp_path}" PATH="{fake_bin}:$PATH"\n'
        f'source "{SIM_ENV}" && sim_setup > /dev/null\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return home


def _sourced(env_sh, command):
    """Run `command` in a clean bash that has sourced only env.sh."""
    result = subprocess.run(
        ["env", "-i", f"PATH=/usr/bin:/bin:{Path(sys.executable).parent}", "bash", "-c",
         f'source "{env_sh / "env.sh"}" && {command}'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_env_sh_runs_the_fakes_from_the_checkout(env_sh):
    # Not from a copy under $NGT_DEV_HOME: from there, nothing else in scenario-player/ is importable.
    found = _sourced(env_sh, "command -v cmsRun")
    assert Path(found).resolve() == (REPO_ROOT / "scenario-player" / "bin" / "cmsRun").resolve()
    assert not (env_sh / "bin").exists()


def test_env_sh_wires_up_fault_injection(env_sh):
    """Without NGT_FAULTS_DIR the fakes never look for armed faults, so a scenario's faults: would be
    silently inert for live_demo.sh's loops; the fake omsapi imports scenario-player/faults.py, which
    only env.sh's PYTHONPATH lets a loop's own Python find."""
    assert _sourced(env_sh, 'echo "$NGT_FAULTS_DIR"') == str(env_sh / "faults")
    found = _sourced(env_sh, f'"{sys.executable}" -c "import omsapi, faults; print(faults.__file__)"')
    assert Path(found).resolve() == (REPO_ROOT / "scenario-player" / "faults.py").resolve()


def test_sim_setup_leaves_an_already_installed_stub_alone(tmp_path):
    """NGT_SIM_STUB_INSTALLED, as in the Airflow container image: no venv needed, and no `pip`, which
    would try to write next to the stub's sources, read-only there."""
    home = tmp_path / "demo-env"
    fake_bin = tmp_path / "fake-pip"
    fake_bin.mkdir()
    pip = fake_bin / "pip"
    pip.write_text("#!/bin/sh\necho pip was run >&2\nexit 1\n", encoding="utf-8")
    pip.chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)
    result = subprocess.run(
        ["env", "-i", f"PATH={fake_bin}:/usr/bin:/bin", f"NGT_DEV_HOME={home}", "NGT_SIM_STUB_INSTALLED=1",
         "NGT_SKIP_LOCAL_ENV=1", "bash", "-c", f'source "{SIM_ENV}" && sim_setup'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "env.sh").is_file()


def _resolved_dev_home(tmp_path, local_sim_env, **env):
    """$NGT_DEV_HOME as a clean bash resolves it from a copy of sim_env.sh that has
    `local_sim_env` beside it -- a scratch checkout, so the real one's file is never touched."""
    scenario_player = tmp_path / "checkout" / "scenario-player"
    scenario_player.mkdir(parents=True, exist_ok=True)
    shutil.copy(SIM_ENV, scenario_player / "sim_env.sh")
    (scenario_player / "local_sim_env.sh").write_text(local_sim_env, encoding="utf-8")
    result = subprocess.run(
        ["env", "-i", "PATH=/usr/bin:/bin", *(f"{k}={v}" for k, v in env.items()), "bash", "-c",
         f'source "{scenario_player / "sim_env.sh"}" && echo "$NGT_DEV_HOME"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_local_sim_env_beats_the_environment_unless_skipped(tmp_path):
    local = 'NGT_DEV_HOME="/from/local_sim_env"\n'
    assert _resolved_dev_home(tmp_path, local, NGT_DEV_HOME="/from/env") == "/from/local_sim_env"
    assert _resolved_dev_home(tmp_path, local, NGT_DEV_HOME="/from/env", NGT_SKIP_LOCAL_ENV="1") == "/from/env"


def test_the_suite_skips_local_env_files():
    """tests/conftest.py's autouse fixture -- what keeps the real scratch environment a developer's
    local_sim_env.sh may name away from the tests that spawn sim_env.sh or live_demo.sh."""
    assert os.environ.get("NGT_SKIP_LOCAL_ENV") == "1"
