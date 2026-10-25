"""
What airflow_demo.sh's four tmux sessions start with. A session's shell inherits the tmux
*server's* environment -- that of whichever shell first started the server -- not the one of the
`tmux new-session` that created it, and airflow_env.sh's defaults are all `${VAR:-default}`. So a
server started by another checkout's `airflow_demo.sh start` would hand that checkout's
AIRFLOW_HOME, database and DAGs folder to this checkout's services unless the script passes its
own. These run the real script against a fake venv whose `airflow` only records its environment,
on a private tmux server -- no Airflow, no database, and the operator's own tmux server untouched.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("tmux") is None,
    reason="needs bash and tmux",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AIRFLOW_DEMO = REPO_ROOT / "airflow_automation" / "airflow_demo" / "airflow_demo.sh"
AIRFLOW_ENV = REPO_ROOT / "airflow_automation" / "airflow_demo" / "airflow_env.sh"
COMPONENTS = ("Scheduler", "DagProcessor", "ApiServer", "Triggerer")
# Settings airflow_env.sh resolves per checkout, i.e. what must never come from somewhere else.
PER_CHECKOUT = (
    "NGT_INSTANCE",
    "NGT_DEV_HOME",
    "AIRFLOW_HOME",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
    "AIRFLOW__CORE__DAGS_FOLDER",
    "AIRFLOW__CORE__EXECUTION_API_SERVER_URL",
    "NGT_AIRFLOW_TMUX_PREFIX",
)


@pytest.fixture
def demo(tmp_path):
    out = tmp_path / "recorded"
    out.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text(f'export VIRTUAL_ENV="{venv}"\nexport PATH="{venv}/bin:$PATH"\n')
    fake_airflow = venv / "bin" / "airflow"
    fake_airflow.write_text(f"#!/bin/bash\nenv > \"{out}/$(tmux display-message -p '#S').env\"\nexec sleep 300\n")
    fake_airflow.chmod(0o755)
    dev_home = tmp_path / "demo-env"
    dev_home.mkdir()
    (dev_home / "env.sh").write_text(f'export NGT_DEV_HOME="{dev_home}"\n')

    # A private tmux server, under a short directory: a tmux socket path has a length limit.
    tmux_dir = tempfile.mkdtemp(prefix="ngt-tmux-")
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path)), "TMUX_TMPDIR": tmux_dir}
    env = dict(
        base,
        NGT_SKIP_LOCAL_ENV="1",
        NGT_AIRFLOW_VENV=str(venv),
        NGT_DEV_HOME=str(dev_home),
        AIRFLOW_HOME=str(tmp_path / "airflow-home"),
    )

    def tmux(*args, extra_env=None):
        return subprocess.run(
            ["tmux", *args], env=dict(base, **(extra_env or {})), capture_output=True, text=True, check=False
        )

    def run(*args):
        return subprocess.run(["bash", str(AIRFLOW_DEMO), *args], env=env, capture_output=True, text=True, check=False)

    def resolved():
        """PER_CHECKOUT as airflow_env.sh itself resolves them, in a clean shell with the same env."""
        script = f'source "{AIRFLOW_ENV}" > /dev/null && ' + " && ".join(f'echo "{v}=${{{v}}}"' for v in PER_CHECKOUT)
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def session_env(session, timeout=20):
        """The environment the fake `airflow` in `session` was started with."""
        path = out / f"{session}.env"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and "PATH=" in path.read_text():
                return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
            time.sleep(0.2)
        pytest.fail(f"{session} never started its fake airflow")

    try:
        yield SimpleNamespace(tmux=tmux, run=run, resolved=resolved, session_env=session_env, env=env)
    finally:
        run("stop")
        tmux("kill-server")
        shutil.rmtree(tmux_dir, ignore_errors=True)


def test_sessions_get_this_checkouts_settings_even_from_another_checkouts_tmux_server(demo):
    # Another checkout's `start` started the tmux server, so its settings are the server's
    # environment -- the default of every shell the server spawns from now on.
    other = {
        "NGT_INSTANCE": "other_checkout",
        "NGT_DEV_HOME": "/other/demo-env",
        "AIRFLOW_HOME": "/other/airflow-home",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "postgresql+psycopg2://u:p@localhost/airflow3_ngt_other_checkout",
        "AIRFLOW__CORE__DAGS_FOLDER": "/other/airflow_automation/airflow_dags",
        "NGT_ONLY_THE_OTHER_CHECKOUT_SETS_THIS": "1",
    }
    demo.tmux("new-session", "-d", "-s", "other-checkouts-session", "sleep 300", extra_env=other)

    started = demo.run("start")
    assert started.returncode == 0, started.stdout + started.stderr

    expected = demo.resolved()
    assert expected["NGT_INSTANCE"] != "other_checkout"
    for component in COMPONENTS:
        got = demo.session_env(expected["NGT_AIRFLOW_TMUX_PREFIX"] + component)
        assert {v: got.get(v) for v in PER_CHECKOUT} == expected, component
        assert "NGT_ONLY_THE_OTHER_CHECKOUT_SETS_THIS" not in got, component


def test_start_leaves_the_tmux_server_without_this_checkouts_settings(demo):
    started = demo.run("start")
    assert started.returncode == 0, started.stdout + started.stderr
    prefix = demo.resolved()["NGT_AIRFLOW_TMUX_PREFIX"]
    assert demo.session_env(prefix + "Scheduler")["AIRFLOW_HOME"] == demo.env["AIRFLOW_HOME"]

    # This `start` is what started the server, and the server would hand its own environment to
    # every shell it spawns -- an operator's new windows, where another checkout's scripts would
    # take an inherited NGT_INSTANCE/AIRFLOW_HOME for their own.
    server_env = demo.tmux("show-environment", "-g").stdout.splitlines()
    assert [line for line in server_env if line.startswith(("NGT_", "AIRFLOW_"))] == []

    # The settings travel in a file instead (they include the database password): owner-only.
    settings = [p for p in Path(demo.env["AIRFLOW_HOME"]).iterdir() if p.is_file()]
    assert settings
    assert {p.name: stat.S_IMODE(p.stat().st_mode) for p in settings} == {p.name: 0o600 for p in settings}


def test_two_dev_homes_of_one_checkout_get_separate_airflow_instances(tmp_path):
    """Airflow home, database and session prefix follow NGT_INSTANCE, which follows NGT_DEV_HOME."""
    names = ("AIRFLOW_HOME", "NGT_AIRFLOW_DB_NAME", "NGT_AIRFLOW_TMUX_PREFIX")

    def resolved(home):
        script = f'source "{AIRFLOW_ENV}" > /dev/null && ' + " && ".join(f'echo "${{{v}}}"' for v in names)
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NGT_SKIP_LOCAL_ENV": "1", "NGT_DEV_HOME": str(home)}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout.split()

    first, second = resolved(tmp_path / "a"), resolved(tmp_path / "b")
    assert resolved(tmp_path / "a") == first
    assert all(one != other for one, other in zip(first, second))
