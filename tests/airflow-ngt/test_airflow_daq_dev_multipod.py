"""
The daq-dev example of the multi-pod deployment (airflow_automation/airflow_containers/daq-dev-multipod/), without a
cluster: that what `kubectl apply -k extras/` and `helm install` create keeps to the rules the tenant's Capsule Tenant
enforces at deploy time (registries, StorageClasses, Ingress class and hostnames, the LimitRange), and that every pod
passes the checks its Kyverno policies report, as test_airflow_daq_dev.py does for the one-pod. Nothing names the
namespace, so any namespace of the tenant gets its own instance, under its own host. The rules are the ones read from the
cluster, in ../daq-dev/README.md.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from helm_chart_fixtures import airflow_chart, render_chart  # noqa: F401 -- pytest's fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
OVERLAY = CONTAINERS / "daq-dev-multipod"

ALLOWED_REGISTRIES = ("harbor.cms", "gitlab-registry.cern.ch")
ALLOWED_HOSTNAMES = re.compile(r".*\.daq-dev\.cms\.cern\.ch$")
# The tenant's LimitRange, per container.
CONTAINER_MIN = {"cpu": 40, "memory": 40}
CONTAINER_MAX = {"cpu": 32000, "memory": 64 * 1024}
# The StorageClasses the Tenant allows, by access mode, and their cap per volume.
STORAGE = {"longhorn": ("ReadWriteOnce", None), "trident-csi-nfs-dedicated": ("ReadWriteMany", 30 * 1024)}
# A daq-dev policy of CMS's own: every emptyDir under this.
EMPTYDIR_MAX = 400

needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl, for its kustomize")


def _extras():
    result = subprocess.run(["kubectl", "kustomize", str(OVERLAY / "extras")], capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _amount(resource, amount):
    """In millicores, or in MiB."""
    if resource == "cpu":
        return int(amount[:-1]) if amount.endswith("m") else int(float(amount) * 1000)
    return int(amount[:-2]) * {"Mi": 1, "Gi": 1024}[amount[-2:]]


def _check_pod(name, pod, labels):
    assert pod["serviceAccountName"] not in (None, "default"), name
    assert pod["automountServiceAccountToken"] is False, name
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
        for resource in ("cpu", "memory"):
            for amounts in (requests, limits):
                assert CONTAINER_MIN[resource] <= _amount(resource, amounts[resource]) <= CONTAINER_MAX[resource], name
        memory = _amount("memory", requests["memory"]), _amount("memory", limits["memory"])
        assert memory[0] < memory[1] <= 2.5 * memory[0], (name, container["name"])
        assert container["securityContext"]["readOnlyRootFilesystem"] is True, (name, container["name"])
    for container in pod["containers"]:
        if "readinessProbe" in container and "livenessProbe" in container:
            assert container["readinessProbe"] != container["livenessProbe"], (name, container["name"])


@needs_kubectl
def test_extras_go_wherever_kubectl_is_pointed():
    for doc in _extras():
        assert "namespace" not in doc["metadata"], (doc["kind"], doc["metadata"]["name"])


@needs_kubectl
def test_volumes_use_classes_the_tenant_allows_in_modes_they_offer():
    claims = [doc for doc in _extras() if doc["kind"] == "PersistentVolumeClaim"]
    assert claims
    for claim in claims:
        mode, cap = STORAGE[claim["spec"]["storageClassName"]]
        assert claim["spec"]["accessModes"] == [mode], claim["metadata"]["name"]
        assert cap is None or _amount("memory", claim["spec"]["resources"]["requests"]["storage"]) <= cap


@needs_kubectl
def test_extras_pods_pass_the_clusters_audited_checks():
    for doc in _extras():
        if doc["kind"] == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            _check_pod(doc["metadata"]["name"], pod, doc["spec"]["template"]["metadata"]["labels"])
            assert pod["imagePullSecrets"] == [{"name": "harbor-ngt-sakura"}]
    service_account = next(doc for doc in _extras() if doc["kind"] == "ServiceAccount")
    assert service_account["automountServiceAccountToken"] is False


@needs_kubectl
def test_both_daq_dev_deployments_run_the_same_image():
    def tags(kustomization):
        images = yaml.safe_load(kustomization.read_text(encoding="utf-8"))["images"]
        return {image["newName"]: image.get("newTag") for image in images}

    onepod = tags(CONTAINERS / "daq-dev" / "kustomization.yaml")
    extras = tags(OVERLAY / "extras" / "kustomization.yaml")
    chart = yaml.safe_load((OVERLAY / "values.yaml").read_text(encoding="utf-8"))["images"]["airflow"]
    assert onepod == extras
    assert onepod[chart["repository"]] == chart["tag"]


@pytest.mark.parametrize("namespace", ["ngt-sakura-airflow-multipod", "ngt-sakura-airflow-second"])
def test_each_namespace_gets_its_own_host(render_chart, namespace):
    docs = render_chart(namespace, CONTAINERS / "multipod-helm" / "values.yaml", OVERLAY / "values.yaml")
    (ingress,) = [doc for doc in docs if doc["kind"] == "Ingress"]
    host = f"{namespace}.daq-dev.cms.cern.ch"
    assert ALLOWED_HOSTNAMES.match(host)
    assert [rule["host"] for rule in ingress["spec"]["rules"]] == [host]
    assert ingress["spec"]["tls"] == [{"hosts": [host], "secretName": "ngt-airflow-tls"}]
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "cms-ca-issuer"
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] in {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Service"}


def test_chart_pods_pass_the_clusters_audited_checks(render_chart):
    docs = render_chart("ngt-sakura-airflow-multipod", CONTAINERS / "multipod-helm" / "values.yaml", OVERLAY / "values.yaml")
    workloads = [doc for doc in docs if doc["kind"] in ("Deployment", "StatefulSet", "Job")]
    assert len(workloads) == 4
    for doc in workloads:
        template = doc["spec"]["template"]
        _check_pod(doc["metadata"]["name"], template["spec"], template["metadata"]["labels"])
        assert template["spec"]["imagePullSecrets"] == [{"name": "harbor-ngt-sakura"}]
    for doc in docs:
        assert {"app.kubernetes.io/name", "cms.cern.ch/division"} <= set(doc["metadata"].get("labels", {})), doc["kind"]
    config = next(doc for doc in docs if doc["kind"] == "ConfigMap")["data"]["airflow.cfg"]
    assert re.search(r"^cookie_secure = True$", config, flags=re.MULTILINE)
    assert re.search(r"^enable_proxy_fix = True$", config, flags=re.MULTILINE)
