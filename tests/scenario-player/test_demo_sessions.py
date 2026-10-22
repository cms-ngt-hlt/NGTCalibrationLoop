"""live_demo.sh only touches its own NGT_INSTANCE's tmux sessions (checked against a fake tmux),
and the default NGT_INSTANCE differs per NGT_DEV_HOME and per checkout."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LIVE_DEMO = Path(__file__).resolve().parents[2] / "scenario-player" / "live_demo.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX bash")

FAKE_TMUX = """#!/bin/bash
S="$FAKE_TMUX_SESSIONS"
case "$1" in
  list-sessions) cat "$S" ;;
  has-session) grep -qx -- "${3#=}" "$S" ;;
  kill-session) grep -vx -- "${3#=}" "$S" > "$S.new"; mv "$S.new" "$S" ;;
esac
"""


def test_stop_all_kills_only_this_instances_sessions(tmp_path):
    (tmp_path / "tmux").write_text(FAKE_TMUX, encoding="utf-8")
    (tmp_path / "tmux").chmod(0o755)
    sessions = tmp_path / "sessions"
    sessions.write_text("NGTDemo_a_2_EcalPedestals\nNGTDemo_b_2_EcalPedestals\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env.update(PATH=f"{tmp_path}:{env['PATH']}", FAKE_TMUX_SESSIONS=str(sessions), NGT_INSTANCE="a")

    subprocess.run(["bash", str(LIVE_DEMO), "stop-all", "EcalPedestals"], env=env, check=True)

    assert sessions.read_text(encoding="utf-8").split() == ["NGTDemo_b_2_EcalPedestals"]


def _instance(checkout, dev_home=None):
    """NGT_INSTANCE as sim_env.sh, copied into a scratch `checkout`, resolves it."""
    sim_env = checkout / "scenario-player" / "sim_env.sh"
    sim_env.parent.mkdir(parents=True, exist_ok=True)
    # create the per-checkout script, it matters because NGT_INSTANCE is based on its path
    shutil.copy(LIVE_DEMO.with_name("sim_env.sh"), sim_env)
    env = {"PATH": "/usr/bin:/bin", **({"NGT_DEV_HOME": str(dev_home)} if dev_home else {})}
    script = f'source "{sim_env}" && echo "$NGT_INSTANCE"'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout


def test_the_instance_differs_per_dev_home_and_per_checkout(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    assert _instance(one, tmp_path / "a") != _instance(one, tmp_path / "b")
    assert _instance(one) != _instance(two)