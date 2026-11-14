"""
The multi-pod deployment in airflow_automation/airflow_containers/multipod-helm/, without a cluster: a chart with
Airflow's own as its dependency, its own templates for what that doesn't deploy, and post-renderer/ for what its values
can't set. That the parts fit together (names, the Secret's keys, the volumes), that every pod runs the strictest way a
cluster can run it, and that the setup script does its job once. What's rendered needs helm and Airflow's chart
(helm_chart_fixtures.py); the values, the script and the post-renderer don't.
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
POST_RENDERER = MULTIPOD / "post-renderer"
# The Secret's keys, as the tutorial creates it.
SECRET_KEYS = {"postgres-password", "connection", "jwt-secret", "api-secret-key", "fernet-key", "admin-password"}

needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash")
needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl, for its kustomize")


def _values():
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))


def _by_name(docs):
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def _workloads(docs):
    return [doc for doc in docs if doc["kind"] in ("Deployment", "StatefulSet", "Job", "CronJob")]


def _check_pod(name, pod):
    """What the restricted Pod Security level and policy engines' best practices ask of a pod, as the one-pod's tests do."""
    assert pod["securityContext"]["runAsNonRoot"] is True, name
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
    assert pod["automountServiceAccountToken"] is False, name
    assert pod["enableServiceLinks"] is False, name
    for volume in pod.get("volumes", []):
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


def test_chart_depends_on_airflows_and_keeps_its_download_out_of_git():
    chart = yaml.safe_load((MULTIPOD / "Chart.yaml").read_text(encoding="utf-8"))
    assert [(d["name"], d["repository"]) for d in chart["dependencies"]] == [("airflow", "https://airflow.apache.org")]
    ignored = subprocess.run(["git", "check-ignore", "-q", str(MULTIPOD / "charts" / "airflow.tgz")], cwd=REPO_ROOT)
    assert ignored.returncode == 0, "helm dependency build downloads Airflow's chart into charts/"


def test_airflow_runs_no_jobs_and_nothing_talks_to_kubernetes():
    airflow = _values()["airflow"]
    assert airflow["executor"] == "LocalExecutor"
    for job in ("migrateDatabaseJob", "createUserJob"):
        assert airflow[job]["enabled"] is False and airflow[job]["serviceAccount"]["create"] is False, job
    assert airflow["rbac"]["create"] is False
    assert airflow["enableServiceLinks"] is False
    for component in ("scheduler", "triggerer", "dagProcessor", "apiServer"):
        assert airflow[component]["serviceAccount"]["automountServiceAccountToken"] is False, component
        assert airflow[component]["resources"]["requests"] and airflow[component]["resources"]["limits"], component
    for unused in ("postgresql", "redis", "statsd", "pgbouncer"):
        assert airflow[unused]["enabled"] is False, unused
    for key in ("fernetKeySecretName", "apiSecretKeySecretName", "jwtSecretName"):
        assert airflow[key] == "ngt-airflow", key
    assert airflow["data"]["metadataSecretName"] == "ngt-airflow"


def test_components_checked_with_airflows_cli_have_room_for_it():
    # Their liveness probes run `airflow jobs check` inside the container: with one CPU, they timed out on kind.
    airflow = _values()["airflow"]
    for component in ("scheduler", "triggerer", "dagProcessor"):
        assert float(airflow[component]["resources"]["limits"]["cpu"]) >= 2, component


def test_the_scheduler_alone_sets_up_and_the_others_wait_for_it():
    airflow = _values()["airflow"]
    # Its own setup migrates: waiting for the migrations first would wait forever.
    assert airflow["scheduler"]["waitForMigrations"]["enabled"] is False
    assert [c["args"] for c in airflow["scheduler"]["extraInitContainers"]] == [["bash", "/opt/ngt-scripts/init_multipod.sh"]]
    assert "$NGT_DEV_HOME/env.sh" in airflow["triggerer"]["extraInitContainers"][0]["args"][-1]


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
        "PYTHONPATH": str(REPO_ROOT),
        "NGT_DEV_HOME": str(tmp_path / "demo-env"),
        "AIRFLOW_HOME": str(tmp_path / "home"),
        "NGT_ADMIN_PASSWORD": "secret",
    }
    for _ in range(2):
        subprocess.run(["bash", str(MULTIPOD / "files" / "init_multipod.sh")], env=env, check=True, capture_output=True)
    made = calls.read_text(encoding="utf-8").splitlines()
    assert [line for line in made if not line.startswith(("airflow db check", "airflow users list"))] == [
        "airflow_demo.sh setup",
        "airflow users create --username admin --role Admin --firstname NGT --lastname Admin "
        "--email admin@example.invalid --password secret",
        "airflow db migrate",
    ]
    # The library, beside $NGT_DEV_HOME, for step pods of another image: each time, so it follows the image.
    assert (tmp_path / "lib" / "ngt_calibration_loop" / "run_step.py").is_file()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("lib")) == ["lib"]


@needs_bash
@needs_kubectl
def test_post_renderer_changes_only_what_airflows_chart_cant_be_told():
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
    docs = _by_name(d for d in yaml.safe_load_all(out.stdout) if d)
    assert set(docs) == {("Deployment", "ngt-api-server"), ("Deployment", "ngt-scheduler"), ("ConfigMap", "ngt-config")}
    for name in ("ngt-api-server", "ngt-scheduler"):
        assert docs[("Deployment", name)]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    api = docs[("Deployment", "ngt-api-server")]["spec"]["template"]["spec"]["containers"][0]
    assert api["livenessProbe"] == {"tcpSocket": {"port": 8080}, "periodSeconds": 10}
    assert api["readinessProbe"]["httpGet"]["path"] == "/api/v2/monitor/health"


@needs_bash
@needs_kubectl
def test_post_renderer_gives_the_scheduler_an_api_token_only_when_it_starts_step_pods():
    rendered = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: ngt-scheduler, annotations: {ngt-airflow/step-pods: "true"}}
spec: {template: {spec: {containers: [{name: scheduler, volumeMounts: []}], volumes: []}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: ngt-triggerer}
spec: {template: {spec: {containers: [{name: triggerer, volumeMounts: []}], volumes: []}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: ngt-api-server}
spec: {template: {spec: {containers: [{name: api-server, livenessProbe: {httpGet: {port: 8080}}}]}}}
---
apiVersion: v1
kind: Secret
metadata: {name: ngt-broker-url}
"""
    out = subprocess.run(["bash", str(POST_RENDERER / "post-render.sh")], input=rendered, capture_output=True, text=True, check=True)
    docs = _by_name(d for d in yaml.safe_load_all(out.stdout) if d)
    scheduler = docs[("Deployment", "ngt-scheduler")]["spec"]["template"]["spec"]
    assert scheduler["automountServiceAccountToken"] is False, "still said by the pod: the token is projected"
    (volume,) = scheduler["volumes"]
    assert [list(source)[0] for source in volume["projected"]["sources"]] == ["serviceAccountToken", "configMap", "downwardAPI"]
    assert scheduler["containers"][0]["volumeMounts"] == [
        {"name": volume["name"], "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount", "readOnly": True}
    ]
    assert docs[("Deployment", "ngt-triggerer")]["spec"]["template"]["spec"]["volumes"] == []


def test_post_renderer_is_a_helm_4_plugin_running_the_script():
    plugin = yaml.safe_load((POST_RENDERER / "plugin.yaml").read_text(encoding="utf-8"))
    assert plugin["type"] == "postrenderer/v1" and plugin["runtime"] == "subprocess"
    command = plugin["runtimeConfig"]["platformCommand"][0]["command"]
    assert command == "${HELM_PLUGIN_DIR}/post-render.sh" and (POST_RENDERER / "post-render.sh").is_file()


def test_as_deployed_every_pod_runs_the_strictest_way(render_chart):
    docs = render_chart("ngt-any")
    kinds = {doc["kind"] for doc in docs}
    # LocalExecutor with no Jobs, Celery or Airflow-made volumes: six Deployments, the scheduler's included.
    assert not kinds & {"Job", "CronJob", "StatefulSet", "Role", "RoleBinding"}
    assert sorted(d["metadata"]["name"] for d in docs if d["kind"] == "Deployment") == [
        "ngt-airflow-postgres", "ngt-airflow-tools", "ngt-api-server", "ngt-dag-processor", "ngt-scheduler", "ngt-triggerer"
    ]
    assert not [d for d in docs if d["kind"] == "Secret"], "the Secret is yours, and the broker's one is dropped"
    for doc in _workloads(docs):
        _check_pod(doc["metadata"]["name"], doc["spec"]["template"]["spec"])
    config = _by_name(docs)[("ConfigMap", "ngt-config")]["data"]["airflow.cfg"]
    assert re.search(r"execution_api_server_url = http://ngt-api-server:8080/execution/", config)


def test_the_history_is_on_volumes_helm_uninstall_leaves(render_chart):
    docs = _by_name(render_chart("ngt-any"))
    claims = {name: doc for (kind, name), doc in docs.items() if kind == "PersistentVolumeClaim"}
    assert {name: doc["spec"]["accessModes"] for name, doc in claims.items()} == {
        "ngt-airflow-postgres": ["ReadWriteOnce"],
        "ngt-airflow-data": ["ReadWriteMany"],
        "ngt-airflow-logs": ["ReadWriteMany"],
    }
    for name, claim in claims.items():
        assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep", name
        assert "storageClassName" not in claim["spec"], "the cluster's default unless your values say"
    # Two pods on one ReadWriteOnce volume can't be on different nodes: no overlap during a rollout.
    assert docs[("Deployment", "ngt-airflow-postgres")]["spec"]["strategy"] == {"type": "Recreate"}


def test_its_own_objects_and_airflows_name_the_same_things(render_chart):
    docs = render_chart("ngt-any")
    claims = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"}
    config_maps = {d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"}
    service_accounts = {d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"}
    for doc in _workloads(docs):
        pod = doc["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] in service_accounts, doc["metadata"]["name"]
        for volume in pod.get("volumes", []):
            if "persistentVolumeClaim" in volume:
                assert volume["persistentVolumeClaim"]["claimName"] in claims, doc["metadata"]["name"]
            if "configMap" in volume:
                assert volume["configMap"]["name"] in config_maps, doc["metadata"]["name"]
        for container in pod.get("initContainers", []) + pod["containers"]:
            for entry in container.get("env", []):
                ref = entry.get("valueFrom", {}).get("secretKeyRef")
                if ref and ref["name"] == "ngt-airflow":
                    assert ref["key"] in SECRET_KEYS, (doc["metadata"]["name"], ref["key"])
    # One image for every pod but Postgres's, set once, under airflow.images.
    images = {c["image"] for doc in _workloads(docs) for c in doc["spec"]["template"]["spec"]["containers"]}
    assert images == {"ngt-airflow:dev", "postgres:16"}


def test_every_pod_can_write_the_volumes_it_mounts(render_chart):
    # A volume on block storage starts out writable by root alone: fsGroup gives it to the group each image runs in.
    pods = {d["metadata"]["name"]: d["spec"]["template"]["spec"] for d in render_chart("ngt-any") if d["kind"] == "Deployment"}
    for name, pod in pods.items():
        if any("persistentVolumeClaim" in volume for volume in pod.get("volumes", [])):
            assert pod["securityContext"]["fsGroup"] == (999 if name == "ngt-airflow-postgres" else 0), name
            assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch", name
    # The tools pod runs Airflow's image on Airflow's volumes.
    assert pods["ngt-airflow-tools"]["securityContext"] == pods["ngt-scheduler"]["securityContext"]


def test_the_tools_pod_runs_where_airflows_pods_may(render_chart, tmp_path):
    values = tmp_path / "nodes.yaml"
    values.write_text("airflow:\n  nodeSelector:\n    disk: shared\n", encoding="utf-8")
    pods = {d["metadata"]["name"]: d["spec"]["template"]["spec"] for d in render_chart("ngt-any", values) if d["kind"] == "Deployment"}
    selected = {name for name, pod in pods.items() if pod.get("nodeSelector") == {"disk": "shared"}}
    assert selected == set(pods) - {"ngt-airflow-postgres"}, "Postgres's volume is its own: it can run anywhere"


def _airflow_env(doc):
    return {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0].get("env", [])}


def test_step_pods_are_off_unless_asked_for(render_chart):
    docs = _by_name(render_chart("ngt-any"))
    assert not {key for key in docs if key[1].startswith("ngt-airflow-step")}
    scheduler = docs[("Deployment", "ngt-scheduler")]["spec"]["template"]["spec"]
    assert not [v for v in scheduler["volumes"] if "projected" in v], "no API token for the scheduler"
    assert "NGT_STEP_POD_TEMPLATE" not in _airflow_env(docs[("Deployment", "ngt-scheduler")])


def test_step_pods_come_from_a_template_only_the_scheduler_may_start(render_chart):
    docs = _by_name(render_chart("ngt-any", MULTIPOD / "values-step-pods.yaml"))
    template = docs[("ConfigMap", "ngt-airflow-step-pod")]["data"]["pod.yaml"]
    pod = yaml.safe_load(template)
    _check_pod("step pod", pod["spec"])
    assert pod["spec"]["restartPolicy"] == "Never"
    (base,) = pod["spec"]["containers"]
    assert base["name"] == "base" and base["image"] == "ngt-airflow:dev", "KubernetesPodOperator's container, Airflow's image"
    assert base["command"][-1] == "run_step" and "python3 -m ngt_calibration_loop.run_step" in base["command"][2]
    assert {"name": "ngt-data", "persistentVolumeClaim": {"claimName": "ngt-airflow-data"}} in pod["spec"]["volumes"]
    assert pod["spec"]["securityContext"] == _values()["airflow"]["securityContexts"]["pod"], "Airflow's, as its image"
    assert ("ConfigMap", "ngt-airflow-step-files") not in docs, "no files unless stepPods.files gives some"
    tools = docs[("Deployment", "ngt-airflow-tools")]["spec"]["template"]["spec"]["containers"][0]
    assert "NGT_REAL_DEPENDENCIES" not in {e["name"] for e in tools["env"]}, "the fakes, unless the values say"
    assert not re.search(r"\{[{%#]", template), "KubernetesPodOperator renders the template with Jinja"

    role = docs[("Role", "ngt-airflow-step-pods")]
    assert {r for rule in role["rules"] for r in rule["resources"]} == {"pods", "pods/log", "events"}
    binding = docs[("RoleBinding", "ngt-airflow-step-pods")]
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "ngt-airflow-scheduler"}]
    assert ("ServiceAccount", "ngt-airflow-scheduler") in docs

    scheduler = docs[("Deployment", "ngt-scheduler")]["spec"]["template"]["spec"]
    assert scheduler["automountServiceAccountToken"] is False
    assert [v for v in scheduler["volumes"] if "projected" in v], "the scheduler gets a projected API token"
    for component in ("ngt-scheduler", "ngt-dag-processor", "ngt-triggerer", "ngt-api-server"):
        ref = _airflow_env(docs[("Deployment", component)])["NGT_STEP_POD_TEMPLATE"]["valueFrom"]["configMapKeyRef"]
        assert ref == {"name": "ngt-airflow-step-pod", "key": "pod.yaml"}, component


def test_step_pods_of_another_image_get_its_user_and_their_files(render_chart, tmp_path):
    values = tmp_path / "other-image.yaml"
    values.write_text(
        "stepPods:\n"
        "  image: registry.example/cmssw:any\n"
        "  securityContext: {runAsNonRoot: true, runAsUser: 1234, seccompProfile: {type: RuntimeDefault}}\n"
        "  files: {setup.sh: 'echo set up'}\n"
        "  realDependencies: [cmssw]\n",
        encoding="utf-8",
    )
    docs = _by_name(render_chart("ngt-any", MULTIPOD / "values-step-pods.yaml", values))
    # Where scenarios are played from, so that the player refuses one needing fake CMSSW.
    tools = docs[("Deployment", "ngt-airflow-tools")]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "NGT_REAL_DEPENDENCIES", "value": "cmssw"} in tools["env"]
    pod = yaml.safe_load(docs[("ConfigMap", "ngt-airflow-step-pod")]["data"]["pod.yaml"])
    _check_pod("step pod", pod["spec"])
    assert pod["spec"]["securityContext"]["runAsUser"] == 1234
    (base,) = pod["spec"]["containers"]
    assert base["image"] == "registry.example/cmssw:any"
    assert docs[("ConfigMap", "ngt-airflow-step-files")]["data"] == {"setup.sh": "echo set up"}
    assert {"name": "step-files", "configMap": {"name": "ngt-airflow-step-files"}} in pod["spec"]["volumes"]
    assert {"name": "step-files", "mountPath": "/opt/ngt-step-files", "readOnly": True} in base["volumeMounts"]


def test_postgres_runs_as_its_own_user_on_a_volume_it_can_write(render_chart):
    pod = _by_name(render_chart("ngt-any"))[("Deployment", "ngt-airflow-postgres")]["spec"]["template"]["spec"]
    assert pod["containers"][0]["securityContext"]["runAsUser"] == pod["securityContext"]["fsGroup"] == 999
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["PGDATA"]["value"].startswith("/var/lib/postgresql/data/")
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "ngt-airflow", "key": "postgres-password"}
