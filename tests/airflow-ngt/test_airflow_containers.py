"""
The container deployments in airflow_automation/airflow_containers/, without a container runtime: what
they ask of a cluster (one enforcing the `restricted` Pod Security level rejects a pod that gets this
wrong, at deploy time, on a cluster you may not have at hand), that the UI stays as closed as intended,
and that airflow_demo.sh behaves inside a container.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
ONEPOD_MANIFEST = CONTAINERS / "onepod" / "ngt-airflow-onepod.yaml"
ONEPOD_COMPOSE = CONTAINERS / "onepod" / "docker-compose.yaml"
AIRFLOW_DEMO = REPO_ROOT / "airflow_automation" / "airflow_demo" / "airflow_demo.sh"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)


def _manifest():
    return {doc["kind"]: doc for doc in yaml.safe_load_all(ONEPOD_MANIFEST.read_text(encoding="utf-8"))}


def _pod_spec():
    return _manifest()["Deployment"]["spec"]["template"]["spec"]


def _container(name):
    return next(c for c in _pod_spec()["containers"] if c["name"] == name)


def _pod_env(name):
    return {e["name"]: e["value"] for e in _container(name)["env"]}


def _compose(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]


def test_manifest_stays_plain_kubernetes():
    # What depends on one cluster goes on top of it, as an overlay.
    assert set(_manifest()) == {"Deployment", "Service"}


def test_pod_meets_the_restricted_pod_security_level():
    pod_context = _pod_spec()["securityContext"]
    assert pod_context["runAsNonRoot"] is True
    assert pod_context["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in _pod_spec()["containers"]:
        context = container["securityContext"]
        assert "privileged" not in context, container["name"]
        assert context["allowPrivilegeEscalation"] is False, container["name"]
        assert context["capabilities"]["drop"] == ["ALL"], container["name"]
        for amounts in (container["resources"]["requests"], container["resources"]["limits"]):
            assert {"cpu", "memory"} <= set(amounts), container["name"]
    assert "hostPath" not in ONEPOD_MANIFEST.read_text(encoding="utf-8")


def test_pod_fixes_a_uid_only_where_the_image_would_run_as_root():
    # runAsNonRoot refuses postgres' USER root. The Airflow image has a numeric user of its own.
    assert _container("postgres")["securityContext"]["runAsUser"] == 999
    assert "runAsUser" not in _container("airflow")["securityContext"]
    for fixed in ("runAsUser", "runAsGroup", "fsGroup"):
        assert fixed not in _pod_spec()["securityContext"]


def test_service_links_cant_clobber_our_own_settings():
    # A Service called ngt-airflow would get NGT_AIRFLOW_PORT=tcp://<ip>:8080 set in every pod,
    # which airflow_env.sh takes for its port and does arithmetic on. Found on a real cluster.
    assert _manifest()["Service"]["metadata"]["name"] == "ngt-airflow"
    assert _pod_spec()["enableServiceLinks"] is False


def test_pod_ui_needs_a_login_and_keeps_the_config_page_closed():
    env = _pod_env("airflow")
    assert env["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS"] == "admin:admin,viewer:viewer"
    assert "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS" not in env
    assert "AIRFLOW__API__EXPOSE_CONFIG" not in env


def test_pod_database_is_reachable_from_the_pod_only():
    # It takes no password (POSTGRES_HOST_AUTH_METHOD=trust), which is only fine on the loopback.
    assert _pod_env("postgres")["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert _container("postgres")["args"] == ["-c", "listen_addresses=localhost"]
    assert "@localhost:" in _pod_env("airflow")["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"]
    assert [p["port"] for p in _manifest()["Service"]["spec"]["ports"]] == [8080]


def test_local_emulation_of_the_pod_matches_the_manifest():
    services = _compose(ONEPOD_COMPOSE)
    assert services["airflow"]["environment"] == _pod_env("airflow")
    assert services["postgres"]["environment"] == _pod_env("postgres")
    assert services["postgres"]["image"] == _container("postgres")["image"]
    assert services["airflow"]["command"] == _container("airflow")["args"]
    assert services["postgres"]["command"] == _container("postgres")["args"]
    assert services["airflow"]["network_mode"] == "service:postgres"
    assert services["postgres"]["ports"] == ["127.0.0.1:${NGT_ONEPOD_PORT:-8099}:8080"]


def test_compose_runs_every_container_as_an_arbitrary_non_root_user():
    for name, service in _compose(ONEPOD_COMPOSE).items():
        uid, gid = service["user"].rsplit(":", 1)
        assert gid == "0", name
        # Not the image's own 50000: a deployment that only works as that UID wouldn't on a cluster that assigns one.
        assert "1000650000" in uid, name
        assert service["cap_drop"] == ["ALL"], name
        assert service["security_opt"] == ["no-new-privileges:true"], name
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), name


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
