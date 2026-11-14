"""
The daq-dev example of the multi-pod deployment (airflow_automation/airflow_containers/daq-dev-multipod/), without a
cluster: that what `helm install` creates keeps to the rules the tenant's Capsule Tenant enforces at deploy time
(registries, StorageClasses, Ingress class and hostnames, the LimitRange), and that every pod passes the checks its Kyverno
policies report, as test_airflow_daq_dev.py does for the one-pod. Nothing names the namespace, so any namespace of the
tenant gets its own instance, under its own host. The rules are the ones read from the cluster, in ../daq-dev/README.md.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml
from helm_chart_fixtures import airflow_chart, render_chart  # noqa: F401 -- pytest's fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
VALUES = CONTAINERS / "daq-dev-multipod" / "values.yaml"

ALLOWED_REGISTRIES = ("harbor.cms", "gitlab-registry.cern.ch")
ALLOWED_HOSTNAMES = re.compile(r".*\.daq-dev\.cms\.cern\.ch$")
# The tenant's LimitRange, per container, with the CPU maximum it had until November 2026 kept as a bound.
CONTAINER_MIN = {"cpu": 40, "memory": 40}
CONTAINER_MAX = {"cpu": 32000, "memory": 64 * 1024}
# The StorageClasses the Tenant allows, by access mode, and their cap per volume.
STORAGE = {"longhorn": ("ReadWriteOnce", None), "trident-csi-nfs-dedicated": ("ReadWriteMany", 30 * 1024)}
# The nodes NetApp's NFS mounts on: the cluster's original ones (10.191.0.x), not those added in October (2026-11-13).
NFS_NODES = {"node.cms.cern.ch/type": "rubu"}
# A daq-dev policy of CMS's own: every emptyDir under this.
EMPTYDIR_MAX = 400


def _amount(resource, amount):
    """In millicores, or in MiB."""
    if resource == "cpu":
        return int(amount[:-1]) if amount.endswith("m") else int(float(amount) * 1000)
    return int(amount[:-2]) * {"Mi": 1, "Gi": 1024}[amount[-2:]]


def _check_pod(name, pod, labels):
    assert pod["serviceAccountName"] not in (None, "default"), name
    assert pod["automountServiceAccountToken"] is False, name
    assert pod["imagePullSecrets"] == [{"name": "harbor-ngt-sakura"}], name
    assert {"app.kubernetes.io/name", "app.kubernetes.io/instance"} <= set(labels), name
    for volume in pod.get("volumes", []):
        if "emptyDir" in volume:
            assert _amount("memory", volume["emptyDir"]["sizeLimit"]) < EMPTYDIR_MAX, (name, volume["name"])
    for forbidden in ("hostNetwork", "hostPID", "hostIPC"):
        assert forbidden not in pod, name
    for container in pod.get("initContainers", []) + pod["containers"]:
        assert container["image"].split("/", 1)[0] in ALLOWED_REGISTRIES, (name, container["image"])
        tag = container["image"].rsplit(":", 1)[1]
        assert tag and tag != "latest" and "/" not in tag, (name, container["image"])
        requests, limits = container["resources"]["requests"], container["resources"]["limits"]
        # A CPU limit may be left out: forbid-cpu-limits even asks for that.
        assert {"cpu", "memory"} <= set(requests) and "memory" in limits, (name, container["name"])
        for resource, amount in [*requests.items(), *limits.items()]:
            assert CONTAINER_MIN[resource] <= _amount(resource, amount) <= CONTAINER_MAX[resource], name
        memory = _amount("memory", requests["memory"]), _amount("memory", limits["memory"])
        assert memory[0] < memory[1] <= 2.5 * memory[0], (name, container["name"])
        assert container["securityContext"]["readOnlyRootFilesystem"] is True, (name, container["name"])
    for container in pod["containers"]:
        if "readinessProbe" in container and "livenessProbe" in container:
            assert container["readinessProbe"] != container["livenessProbe"], (name, container["name"])


def test_both_daq_dev_deployments_run_the_same_image():
    onepod = yaml.safe_load((CONTAINERS / "daq-dev" / "kustomization.yaml").read_text(encoding="utf-8"))["images"]
    tags = {image["newName"]: image.get("newTag") for image in onepod}
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    airflow = values["airflow"]["images"]["airflow"]
    assert tags[airflow["repository"]] == airflow["tag"]
    assert values["postgres"]["image"]["repository"] in tags


def test_everything_goes_wherever_helm_is_pointed(render_chart):
    for doc in render_chart("ngt-sakura-anything", VALUES):
        assert "namespace" not in doc["metadata"], (doc["kind"], doc["metadata"]["name"])
        assert {"app.kubernetes.io/name", "cms.cern.ch/division"} <= set(doc["metadata"].get("labels", {})), doc["kind"]


def test_volumes_use_classes_the_tenant_allows_in_modes_they_offer(render_chart):
    claims = [doc for doc in render_chart("ngt-sakura-anything", VALUES) if doc["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 3
    for claim in claims:
        mode, cap = STORAGE[claim["spec"]["storageClassName"]]
        assert claim["spec"]["accessModes"] == [mode], claim["metadata"]["name"]
        assert cap is None or _amount("memory", claim["spec"]["resources"]["requests"]["storage"]) <= cap


def test_every_pod_on_nfs_runs_on_a_node_that_can_mount_it(render_chart):
    docs = render_chart("ngt-sakura-anything", VALUES)
    nfs = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim" and d["spec"]["storageClassName"] == "trident-csi-nfs-dedicated"}
    on_nfs = {}
    for doc in docs:
        if doc["kind"] == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            if {v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes", []) if "persistentVolumeClaim" in v} & nfs:
                on_nfs[doc["metadata"]["name"]] = pod.get("nodeSelector")
    assert on_nfs == dict.fromkeys(["ngt-airflow-tools", "ngt-api-server", "ngt-dag-processor", "ngt-scheduler", "ngt-triggerer"], NFS_NODES)


@pytest.mark.parametrize("namespace", ["ngt-sakura-airflow-multipod", "ngt-sakura-airflow-second"])
def test_each_namespace_gets_its_own_host(render_chart, namespace):
    docs = render_chart(namespace, VALUES)
    (ingress,) = [doc for doc in docs if doc["kind"] == "Ingress"]
    host = f"{namespace}.daq-dev.cms.cern.ch"
    assert ALLOWED_HOSTNAMES.match(host)
    assert [rule["host"] for rule in ingress["spec"]["rules"]] == [host]
    assert ingress["spec"]["tls"] == [{"hosts": [host], "secretName": "ngt-airflow-tls"}]
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "cms-ca-issuer"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] in {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Service"}


def test_step_pods_pass_the_clusters_checks_and_run_where_nfs_mounts(render_chart):
    docs = render_chart("ngt-sakura-anything", VALUES, CONTAINERS / "multipod-helm" / "values-step-pods.yaml")
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "ngt-airflow-step-pod")
    pod = yaml.safe_load(config["data"]["pod.yaml"])
    _check_pod("step pod", pod["spec"], pod["metadata"]["labels"])
    assert pod["spec"]["nodeSelector"] == NFS_NODES


CMSSW_VALUES = CONTAINERS / "daq-dev-multipod" / "values-cmssw.yaml"


def test_cmssw_step_pods_pass_the_clusters_checks_with_the_release_from_the_nodes(render_chart):
    docs = render_chart("ngt-sakura-anything", VALUES, CONTAINERS / "multipod-helm" / "values-step-pods.yaml", CMSSW_VALUES)
    by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    template = by_name[("ConfigMap", "ngt-airflow-step-pod")]["data"]["pod.yaml"]
    pod, files = yaml.safe_load(template), by_name[("ConfigMap", "ngt-airflow-step-files")]["data"]
    assert not re.search(r"\{[{%#]", template), "KubernetesPodOperator renders the template with Jinja"
    _check_pod("step pod", pod["spec"], pod["metadata"]["labels"])
    (base,) = pod["spec"]["containers"]
    assert base["image"].startswith("harbor.cms/it-registry-proxy/docker.io/cmssw/el8:") and "@sha256:" in base["image"]
    assert "cpu" not in base["resources"]["limits"]
    # Airflow's user: the scheduler writes in the same runs' directories.
    assert pod["spec"]["securityContext"]["runAsUser"] == 50000
    claims = {(d["metadata"]["name"], d["spec"]["resources"]["requests"]["storage"]) for d in docs if d["kind"] == "PersistentVolumeClaim"}
    assert ("ngt-airflow-data", "20Gi") in claims, "room for the inputs and the runs' outputs"
    tools = by_name[("Deployment", "ngt-airflow-tools")]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "NGT_REAL_DEPENDENCIES", "value": "cmssw"} in tools["env"], "fake scenarios refused here"
    # The el8 install was made under /opt/offline, and only works mounted there.
    (offline,) = [v for v in pod["spec"]["volumes"] if "hostPath" in v]
    assert offline["hostPath"]["path"] == "/opt/offline-el8"
    assert {"name": offline["name"], "mountPath": "/opt/offline", "readOnly": True} in base["volumeMounts"]
    assert pod["spec"]["nodeSelector"] == NFS_NODES, "NFS, and the el8 volume, are on rubu nodes"
    assert pod["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert set(files) == {"frontier_proxy.py", "site-local-config.xml", "storage.json"}
    assert '<proxy url="http://localhost:3128"/>' in files["site-local-config.xml"]
    json.loads(files["storage.json"])


def test_the_cmssw_step_script_is_valid_bash():
    script = yaml.safe_load(CMSSW_VALUES.read_text(encoding="utf-8"))["stepPods"]["script"]
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_the_frontier_forwarder_puts_the_port_back_into_the_host_header(tmp_path):
    """daq-dev's P5 proxy answers 400 to Frontier's own requests: their Host header has no port. The step pods send
    them through frontier_proxy.py, which takes the Host from the URL."""
    source = yaml.safe_load(CMSSW_VALUES.read_text(encoding="utf-8"))["stepPods"]["files"]["frontier_proxy.py"]
    (tmp_path / "frontier_proxy.py").write_text(source, encoding="utf-8")
    seen = []
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)

    def answer():
        conn, _ = upstream.accept()
        with conn:
            seen.append(conn.recv(65536))
            conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nconditions")

    threading.Thread(target=answer, daemon=True).start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        listen_port = probe.getsockname()[1]
    forwarder = subprocess.Popen(
        [sys.executable, str(tmp_path / "frontier_proxy.py"), "127.0.0.1", str(upstream.getsockname()[1]), str(listen_port)],
        stdout=subprocess.PIPE,
    )
    try:
        forwarder.stdout.readline()  # it says it's listening
        with socket.create_connection(("127.0.0.1", listen_port), timeout=10) as client:
            client.sendall(b"GET http://cmsfrontier.cern.ch:8000/FrontierProd/x HTTP/1.0\r\nHost: cmsfrontier.cern.ch\r\n\r\n")
            reply = client.recv(65536)
    finally:
        forwarder.kill()
        upstream.close()
    assert reply.startswith(b"HTTP/1.0 200")
    assert b"Host: cmsfrontier.cern.ch:8000\r\n" in seen[0] and b"Host: cmsfrontier.cern.ch\r\n" not in seen[0]


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash")
def test_an_input_goes_up_in_parts_and_only_once(tmp_path):
    """upload_input.sh, through a stand-in kubectl that runs each command here, with the volume in tmp_path."""
    bin_dir, volume = tmp_path / "bin", tmp_path / "volume"
    bin_dir.mkdir()
    volume.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done; shift\n'
        'args=(); for a in "$@"; do args+=("${a//\\/ngt-data/$VOLUME}"); done\nexec "${args[@]}"\n',
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    data = os.urandom(2 * 1024 * 1024 + 123)
    (tmp_path / "run403634.root").write_bytes(data)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "VOLUME": str(volume), "NS": "ngt-any", "PART_MB": "1"}

    def upload(*name):
        script = CONTAINERS / "daq-dev-multipod" / "upload_input.sh"
        args = ["bash", str(script), str(tmp_path / "run403634.root"), *name]
        return subprocess.run(args, env=env, capture_output=True, text=True, check=True).stdout

    assert upload().count("part ") == 3
    assert (volume / "inputs" / "run403634.root").read_bytes() == data
    assert [p.name for p in (volume / "inputs").iterdir()] == ["run403634.root"]
    assert "there already" in upload()
    upload("370f0750.root")  # the name a scenario gives it
    assert (volume / "inputs" / "370f0750.root").read_bytes() == data


def test_every_pod_passes_the_clusters_audited_checks(render_chart):
    docs = render_chart("ngt-sakura-airflow-multipod", VALUES)
    workloads = [doc for doc in docs if doc["kind"] in ("Deployment", "StatefulSet", "Job")]
    assert len(workloads) == 6
    for doc in workloads:
        template = doc["spec"]["template"]
        _check_pod(doc["metadata"]["name"], template["spec"], template["metadata"]["labels"])
    config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "ngt-config")
    assert re.search(r"^cookie_secure = True$", config["data"]["airflow.cfg"], flags=re.MULTILINE)
    assert re.search(r"^enable_proxy_fix = True$", config["data"]["airflow.cfg"], flags=re.MULTILINE)
