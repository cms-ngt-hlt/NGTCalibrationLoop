"""
The multi-pod deployment in airflow_automation/airflow_containers/multipod-helm/, without a cluster: Airflow's own chart
(values.yaml), post-renderer/ for what its values can't set, and extras/ for what it doesn't deploy. That the parts fit
together (names, the Secret's keys, the volumes), that every pod runs the strictest way a cluster can run it, and that
the setup script does its job once. The chart's half needs helm and Airflow's chart (helm_chart_fixtures.py); the rest
doesn't.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from helm_chart_fixtures import airflow_chart, render_chart  # noqa: F401 -- pytest's fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
MULTIPOD = REPO_ROOT / "airflow_automation" / "airflow_containers" / "multipod-helm"
VALUES = MULTIPOD / "values.yaml"
EXTRAS = MULTIPOD / "extras"
POST_RENDERER = MULTIPOD / "post-renderer"
# The Secret's keys, as the tutorial creates it.
SECRET_KEYS = {"postgres-password", "connection", "jwt-secret", "api-secret-key", "fernet-key", "admin-password"}

needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash")
needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl, for its kustomize")


def _values():
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))


def _extras():
    docs = yaml.safe_load_all((EXTRAS / "ngt-airflow-extras.yaml").read_text(encoding="utf-8"))
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def _workloads(docs):
    return [doc for doc in docs if doc["kind"] in ("Deployment", "StatefulSet", "Job", "CronJob")]


def _check_pod(name, pod):
    """What the restricted Pod Security level and policy engines' best practices ask of a pod, as the one-pod's tests do."""
    assert pod["securityContext"]["runAsNonRoot"] is True, name
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
    assert pod["automountServiceAccountToken"] is False, name
    assert pod["enableServiceLinks"] is False, name
    volumes = {v["name"]: v for v in pod.get("volumes", [])}
    for volume in volumes.values():
        kind = (set(volume) - {"name"}).pop()
        assert kind in ("emptyDir", "persistentVolumeClaim", "configMap", "secret"), (name, volume["name"])
        if kind == "emptyDir":
            assert volume["emptyDir"]["sizeLimit"], (name, volume["name"])
    for container in pod.get("initContainers", []) + pod["containers"]:
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False, (name, container["name"])
        assert context["capabilities"]["drop"] == ["ALL"], (name, container["name"])
        assert context["readOnlyRootFilesystem"] is True, (name, container["name"])
        assert "privileged" not in context, (name, container["name"])
        for amounts in (container["resources"]["requests"], container["resources"]["limits"]):
            assert {"cpu", "memory"} <= set(amounts), (name, container["name"])
    for container in pod["containers"]:
        probes = {p: container[p] for p in ("startupProbe", "readinessProbe", "livenessProbe") if p in container}
        assert probes, (name, container["name"])
        if "readinessProbe" in probes and "livenessProbe" in probes:
            assert probes["readinessProbe"] != probes["livenessProbe"], (name, container["name"])


def test_extras_stay_plain_kubernetes():
    # What depends on one cluster goes on top, as an overlay: see ../daq-dev-multipod/extras.
    assert {kind for kind, _ in _extras()} == {"PersistentVolumeClaim", "Deployment", "Service"}
    for doc in _extras().values():
        assert "namespace" not in doc["metadata"]
        if doc["kind"] == "PersistentVolumeClaim":
            assert "storageClassName" not in doc["spec"]


def test_postgres_keeps_its_volume_to_one_node_and_the_rest_share_theirs():
    claims = {name: doc["spec"]["accessModes"] for (kind, name), doc in _extras().items() if kind == "PersistentVolumeClaim"}
    assert claims == {
        "ngt-airflow-postgres": ["ReadWriteOnce"],
        "ngt-airflow-data": ["ReadWriteMany"],
        "ngt-airflow-logs": ["ReadWriteMany"],
    }
    # Two pods on one ReadWriteOnce volume can't be on different nodes: no overlap during a rollout.
    assert _extras()[("Deployment", "ngt-airflow-postgres")]["spec"]["strategy"] == {"type": "Recreate"}


def test_extras_pods_meet_the_restricted_level_and_write_only_to_their_volumes():
    for doc in _workloads(_extras().values()):
        _check_pod(doc["metadata"]["name"], doc["spec"]["template"]["spec"])


def test_postgres_runs_as_its_own_user_on_a_volume_it_can_write():
    pod = _extras()[("Deployment", "ngt-airflow-postgres")]["spec"]["template"]["spec"]
    assert pod["containers"][0]["securityContext"]["runAsUser"] == pod["securityContext"]["fsGroup"] == 999
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["PGDATA"]["value"].startswith("/var/lib/postgresql/data/")
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "ngt-airflow", "key": "postgres-password"}


def test_chart_and_extras_name_the_same_things():
    values = _values()
    claims = {name for kind, name in _extras() if kind == "PersistentVolumeClaim"}
    assert values["logs"]["persistence"]["existingClaim"] in claims
    for component in ("scheduler", "triggerer"):
        for volume in values[component]["extraVolumes"]:
            if "persistentVolumeClaim" in volume:
                assert volume["persistentVolumeClaim"]["claimName"] in claims, component
    generated = yaml.safe_load((EXTRAS / "kustomization.yaml").read_text(encoding="utf-8"))["configMapGenerator"]
    scripts = next(v for v in values["scheduler"]["extraVolumes"] if "configMap" in v)["configMap"]["name"]
    assert [g["name"] for g in generated] == [scripts]
    assert generated[0]["options"]["disableNameSuffixHash"] is True, "the values name it as it is"
    # One Secret for everything, and only keys the tutorial creates.
    for key in ("data", "fernetKeySecretName", "apiSecretKeySecretName", "jwtSecretName"):
        assert (values[key]["metadataSecretName"] if key == "data" else values[key]) == "ngt-airflow"
    referenced = [values["scheduler"]["extraInitContainers"][0]["env"]]
    referenced += [c.get("env", []) for doc in _workloads(_extras().values()) for c in doc["spec"]["template"]["spec"]["containers"]]
    for env in referenced:
        for entry in env:
            if "valueFrom" in entry:
                assert entry["valueFrom"]["secretKeyRef"]["name"] == "ngt-airflow"
                assert entry["valueFrom"]["secretKeyRef"]["key"] in SECRET_KEYS


def test_chart_runs_no_jobs_and_nothing_talks_to_kubernetes():
    values = _values()
    assert values["executor"] == "LocalExecutor"
    for job in ("migrateDatabaseJob", "createUserJob"):
        assert values[job]["enabled"] is False and values[job]["serviceAccount"]["create"] is False, job
    assert values["rbac"]["create"] is False
    assert values["enableServiceLinks"] is False
    for component in ("scheduler", "triggerer", "dagProcessor", "apiServer"):
        assert values[component]["serviceAccount"]["automountServiceAccountToken"] is False, component
        assert values[component]["resources"]["requests"] and values[component]["resources"]["limits"], component
    for unused in ("postgresql", "redis", "statsd", "pgbouncer"):
        assert values[unused]["enabled"] is False, unused


def test_the_scheduler_alone_sets_up_and_the_others_wait_for_it():
    values = _values()
    # Its own setup migrates: waiting for the migrations first would wait forever.
    assert values["scheduler"]["waitForMigrations"]["enabled"] is False
    setup = values["scheduler"]["extraInitContainers"]
    assert [c["args"] for c in setup] == [["bash", "/opt/ngt-scripts/init_multipod.sh"]]
    assert '$NGT_DEV_HOME/env.sh' in values["triggerer"]["extraInitContainers"][0]["args"][-1]
    tools = _extras()[("Deployment", "ngt-airflow-tools")]["spec"]["template"]["spec"]["containers"][0]
    assert '$NGT_DEV_HOME/env.sh' in tools["readinessProbe"]["exec"]["command"][-1]


@needs_bash
def test_setup_script_sets_up_the_first_time_and_only_migrates_after(tmp_path):
    # Fake airflow and airflow_demo.sh, which record what they're asked, and a database with or without the admin.
    calls = tmp_path / "calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "airflow").write_text(
        "#!/bin/bash\n"
        f'echo "airflow $*" >> {calls}\n'
        'if [ "$1 $2" = "users list" ]; then\n'
        f'  if grep -q "users create" {calls}; then echo \'[{{"username": "admin"}}]\'; else echo "[]"; fi\n'
        "fi\n",
        encoding="utf-8",
    )
    (bin_dir / "airflow_demo.sh").write_text(
        f'#!/bin/bash\necho "airflow_demo.sh $*" >> {calls}\nmkdir -p "$NGT_DEV_HOME" && touch "$NGT_DEV_HOME/env.sh"\n',
        encoding="utf-8",
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    (tmp_path / "home" / "logs").mkdir(parents=True)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NGT_DEV_HOME": str(tmp_path / "demo-env"),
        "AIRFLOW_HOME": str(tmp_path / "home"),
        "NGT_ADMIN_PASSWORD": "secret",
    }
    for _ in range(2):
        subprocess.run(["bash", str(EXTRAS / "init_multipod.sh")], env=env, check=True, capture_output=True)
    made = calls.read_text(encoding="utf-8").splitlines()
    assert [line for line in made if not line.startswith(("airflow db check", "airflow users list"))] == [
        "airflow_demo.sh setup",
        "airflow users create --username admin --role Admin --firstname NGT --lastname Admin "
        "--email admin@example.invalid --password secret",
        "airflow db migrate",
    ]


@needs_bash
@needs_kubectl
def test_post_renderer_changes_only_what_the_chart_cant_be_told():
    rendered = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: ngt-api-server}
spec:
  template:
    spec:
      containers:
        - name: api-server
          readinessProbe: {httpGet: {path: /api/v2/monitor/health, port: 8080}, periodSeconds: 10}
          livenessProbe: {httpGet: {path: /api/v2/monitor/health, port: 8080}, periodSeconds: 10}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: ngt-scheduler}
spec: {template: {spec: {containers: [{name: scheduler}]}}}
---
apiVersion: v1
kind: Secret
metadata: {name: ngt-broker-url}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: ngt-config}
data: {airflow.cfg: ""}
"""
    out = subprocess.run(["bash", str(POST_RENDERER / "post-render.sh")], input=rendered, capture_output=True, text=True, check=True)
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out.stdout) if d}
    assert set(docs) == {("Deployment", "ngt-api-server"), ("Deployment", "ngt-scheduler"), ("ConfigMap", "ngt-config")}
    for name in ("ngt-api-server", "ngt-scheduler"):
        assert docs[("Deployment", name)]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    api = docs[("Deployment", "ngt-api-server")]["spec"]["template"]["spec"]["containers"][0]
    assert api["livenessProbe"] == {"tcpSocket": {"port": 8080}, "periodSeconds": 10}
    assert api["readinessProbe"]["httpGet"]["path"] == "/api/v2/monitor/health"


def test_post_renderer_is_a_helm_4_plugin_running_the_script():
    plugin = yaml.safe_load((POST_RENDERER / "plugin.yaml").read_text(encoding="utf-8"))
    assert plugin["type"] == "postrenderer/v1" and plugin["runtime"] == "subprocess"
    command = plugin["runtimeConfig"]["platformCommand"][0]["command"]
    assert command == "${HELM_PLUGIN_DIR}/post-render.sh" and (POST_RENDERER / "post-render.sh").is_file()


def test_chart_as_deployed_runs_every_pod_the_strictest_way(render_chart):
    docs = render_chart("ngt-any", VALUES)
    kinds = {doc["kind"] for doc in docs}
    # LocalExecutor with no Jobs, Celery or volumes of the chart's own: four Deployments, the scheduler's included.
    assert not kinds & {"Job", "CronJob", "StatefulSet", "PersistentVolumeClaim", "Role", "RoleBinding"}
    assert sorted(d["metadata"]["name"] for d in docs if d["kind"] == "Deployment") == [
        "ngt-api-server", "ngt-dag-processor", "ngt-scheduler", "ngt-triggerer"
    ]
    assert not [d for d in docs if d["kind"] == "Secret"], "the Secret is yours, and the broker's one is dropped"
    for doc in _workloads(docs):
        _check_pod(doc["metadata"]["name"], doc["spec"]["template"]["spec"])
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "ngt-config")
    assert re.search(r"execution_api_server_url = http://ngt-api-server:8080/execution/", config["data"]["airflow.cfg"])
