"""
The daq-dev example in airflow_automation/airflow_containers/daq-dev/, without a cluster: that what
`kubectl apply -k` creates keeps to the rules the tenant's Capsule Tenant enforces at deploy time
(registries, namespace prefix, Ingress class and hostnames, the LimitRange), on a cluster few people can
reach, and that the pod passes the checks its Kyverno policies report, which the cluster only audits
for now but will enforce on staging and production. The rules are the ones read from the cluster on
2026-11-06, in the overlay's README.md.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "airflow_automation" / "airflow_containers" / "daq-dev"

ALLOWED_REGISTRIES = ("harbor.cms", "gitlab-registry.cern.ch")
ALLOWED_HOSTNAMES = re.compile(r".*\.daq-dev\.cms\.cern\.ch$")
# The tenant's LimitRange, per container, and a default limit of 1Gi for a container that sets none.
CONTAINER_MIN = {"cpu": 40, "memory": 40}
CONTAINER_MAX = {"cpu": 32000, "memory": 64 * 1024}

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl, for its kustomize")


def _rendered():
    """What `kubectl apply -k` would create, by kind."""
    result = subprocess.run(["kubectl", "kustomize", str(OVERLAY)], capture_output=True, text=True, check=True)
    return {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout)}


def _pod(rendered):
    return rendered["Deployment"]["spec"]["template"]["spec"]


def _amount(resource, amount):
    """In millicores, or in MiB."""
    if resource == "cpu":
        return int(amount[:-1]) if amount.endswith("m") else int(float(amount) * 1000)
    return int(amount[:-2]) * {"Mi": 1, "Gi": 1024}[amount[-2:]]


def test_everything_is_in_one_of_the_tenants_namespaces():
    rendered = _rendered()
    assert set(rendered) == {"ServiceAccount", "Service", "Deployment", "Ingress"}
    for kind, resource in rendered.items():
        assert resource["metadata"]["namespace"].startswith("ngt-sakura-"), kind


def test_images_come_from_the_allowed_registries_with_a_pinned_tag():
    for container in _pod(_rendered())["containers"]:
        image = container["image"]
        assert image.split("/", 1)[0] in ALLOWED_REGISTRIES, image
        tag = image.rsplit(":", 1)[1]
        assert tag and tag != "latest" and "/" not in tag, image


def test_ingress_is_one_the_tenant_allows():
    rendered = _rendered()
    ingress = rendered["Ingress"]
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "cms-ca-issuer"
    hosts = [rule["host"] for rule in ingress["spec"]["rules"]]
    assert hosts == ingress["spec"]["tls"][0]["hosts"]
    for host in hosts:
        # One label under the wildcard, which is what its DNS and certificates cover.
        assert ALLOWED_HOSTNAMES.match(host) and host.count(".") == 4, host
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == rendered["Service"]["metadata"]["name"]
    assert backend["port"]["name"] in [port["name"] for port in rendered["Service"]["spec"]["ports"]]


def test_each_container_sets_its_own_resources_within_the_limitrange():
    # A container that set none would get a 1Gi memory limit, and airflow has peaked at about 1.5 GiB.
    for container in _pod(_rendered())["containers"]:
        for amounts in (container["resources"]["requests"], container["resources"]["limits"]):
            for resource in ("cpu", "memory"):
                value = _amount(resource, amounts[resource])
                assert CONTAINER_MIN[resource] <= value <= CONTAINER_MAX[resource], (container["name"], resource)


def test_pod_runs_as_its_own_serviceaccount_without_an_api_token():
    rendered = _rendered()
    pod = _pod(rendered)
    assert pod["serviceAccountName"] == rendered["ServiceAccount"]["metadata"]["name"] != "default"
    assert pod["automountServiceAccountToken"] is False
    assert rendered["ServiceAccount"]["automountServiceAccountToken"] is False


def test_pod_passes_the_clusters_audited_checks():
    pod = _pod(_rendered())
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert set(pod["securityContext"]) >= {"runAsNonRoot", "seccompProfile"}
    for container in pod["containers"]:
        context = container["securityContext"]
        assert context["readOnlyRootFilesystem"] is True, container["name"]
        assert context["allowPrivilegeEscalation"] is False, container["name"]
        assert {"livenessProbe", "readinessProbe"} <= set(container), container["name"]
        limits, requests = container["resources"]["limits"], container["resources"]["requests"]
        assert limits["memory"] != requests["memory"], container["name"]
        assert _amount("memory", limits["memory"]) <= 2.5 * _amount("memory", requests["memory"]), container["name"]
        for mount in container["volumeMounts"]:
            assert volumes[mount["name"]]["emptyDir"]["sizeLimit"], mount["name"]
    for forbidden in ("hostNetwork", "hostPID", "hostIPC"):
        assert forbidden not in pod


def test_labels_are_the_ones_asked_for_and_the_selector_is_untouched():
    rendered = _rendered()
    deployment = rendered["Deployment"]
    asked = {
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/part-of",
        "app.kubernetes.io/env",
        "app.kubernetes.io/version",
        "app.kubernetes.io/managed-by",
        "cms.cern.ch/division",
    }
    assert asked <= set(deployment["spec"]["template"]["metadata"]["labels"])
    for kind, resource in rendered.items():
        assert asked <= set(resource["metadata"]["labels"]), kind
    # A selector can't change once the Deployment exists, so `kubectl apply` would fail on a pod made before.
    assert deployment["spec"]["selector"] == {"matchLabels": {"app": "ngt-airflow"}}
    assert rendered["Service"]["spec"]["selector"] == {"app": "ngt-airflow"}


def test_login_works_behind_the_ingress_tls_and_the_base_settings_survive():
    rendered = _rendered()
    pod = _pod(rendered)
    env = {e["name"]: e["value"] for e in next(c for c in pod["containers"] if c["name"] == "airflow")["env"]}
    assert env["AIRFLOW__FAB__AUTH_ROLE_PUBLIC"] == "Viewer"
    assert env["AIRFLOW__FAB__ENABLE_PROXY_FIX"] == env["AIRFLOW__FAB__COOKIE_SECURE"] == "True"
    assert pod["enableServiceLinks"] is False
    assert pod["imagePullSecrets"] == [{"name": "harbor-ngt-sakura"}]
