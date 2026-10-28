"""
Tests what sim_env.sh's sim_setup writes into $NGT_DEV_HOME/env.sh -- the one file every live
adapter (scenario-player/live_demo.sh's loops among them) sources for the fake environment. Like
tests/scenario-player/test_fake_toolchain_isolation.py these spawn a real bash, since what matters is what a
shell sees after sourcing it.
"""

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
