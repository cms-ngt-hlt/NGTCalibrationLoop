"""
The OpenShift example in airflow_automation/airflow_containers/openshift/, without a cluster: that what
`oc apply -k` creates asks for nothing OpenShift's restricted SCC would reject (it rejects a pod that
gets this wrong, at deploy time, on a cluster you may not have at hand), that the pod fits a project's
default quota, and that the Route stays as closed as intended.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = REPO_ROOT / "airflow_automation" / "airflow_containers"
OVERLAY = CONTAINERS / "openshift"
ONEPOD_MANIFEST = CONTAINERS / "onepod" / "ngt-airflow-onepod.yaml"

needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl, for its kustomize")


def _by_kind(text):
    return {doc["kind"]: doc for doc in yaml.safe_load_all(text)}


def _resources():
    return _by_kind((OVERLAY / "ngt-airflow-openshift.yaml").read_text(encoding="utf-8"))


def _rendered():
    """What `oc apply -k` would create."""
    result = subprocess.run(["kubectl", "kustomize", str(OVERLAY)], capture_output=True, text=True, check=True)
    return _by_kind(result.stdout)


def _base_containers():
    deployment = _by_kind(ONEPOD_MANIFEST.read_text(encoding="utf-8"))["Deployment"]
    return deployment["spec"]["template"]["spec"]["containers"]


def test_overlay_drops_every_uid_the_base_fixes():
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text(encoding="utf-8"))
    patch = yaml.safe_load(kustomization["patches"][0]["patch"])
    patched = {c["name"]: c for c in patch["spec"]["template"]["spec"]["containers"]}
    fixed = [c["name"] for c in _base_containers() if "runAsUser" in c["securityContext"]]
    assert fixed
    for name in fixed:
        assert patched[name]["securityContext"]["runAsUser"] is None, name


@needs_kubectl
def test_rendered_pod_asks_for_nothing_the_restricted_scc_would_reject():
    pod = _rendered()["Deployment"]["spec"]["template"]["spec"]
    for context in [pod["securityContext"]] + [c["securityContext"] for c in pod["containers"]]:
        for forbidden in ("runAsUser", "runAsGroup", "fsGroup", "privileged"):
            assert forbidden not in context
    for container in pod["containers"]:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False, container["name"]
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"], container["name"]


@needs_kubectl
def test_rendered_overlay_is_the_base_plus_what_this_cluster_needs():
    rendered = _rendered()
    assert set(rendered) == {"ImageStream", "BuildConfig", "Deployment", "Service", "Route"}
    images = {c["name"]: c["image"] for c in rendered["Deployment"]["spec"]["template"]["spec"]["containers"]}
    assert images["postgres"] == "registry.cern.ch/docker.io/library/postgres:16"
    triggers = json.loads(rendered["Deployment"]["metadata"]["annotations"]["image.openshift.io/triggers"])
    assert triggers[0]["from"] == {"kind": "ImageStreamTag", "name": "ngt-airflow:latest"}
    assert '@.name=="airflow"' in triggers[0]["fieldPath"]


def test_build_finds_the_dockerfile():
    strategy = _resources()["BuildConfig"]["spec"]["strategy"]["dockerStrategy"]
    assert (REPO_ROOT / strategy["dockerfilePath"]).is_file()


def test_route_stays_on_the_cern_network():
    route = _resources()["Route"]
    # On CERN's clusters a Route without this annotation is CERN-only, and '' opens it to the Internet.
    assert "haproxy.router.openshift.io/ip_whitelist" not in route["metadata"].get("annotations", {})
    assert route["spec"]["tls"] == {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}


def _millicores(amount):
    return int(amount[:-1]) if amount.endswith("m") else int(float(amount) * 1000)


def _mebibytes(amount):
    return int(amount[:-2]) * {"Mi": 1, "Gi": 1024}[amount[-2:]]


def test_pod_fits_the_default_quota_with_room_for_a_build():
    # A project's default quota counts requests: 1 CPU, 4 GiB. A build requests 50m, 256Mi by default.
    requests = [c["resources"]["requests"] for c in _base_containers()]
    assert sum(_millicores(r["cpu"]) for r in requests) + 50 <= 1000
    assert sum(_mebibytes(r["memory"]) for r in requests) + 256 <= 4096
