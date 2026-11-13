"""
Fixtures for the tests of the deployment built on Airflow's own Helm chart (airflow_automation/airflow_containers/
multipod-helm/, a chart with Airflow's as its dependency): Airflow's chart itself, and what the whole renders through the
post-renderer, as `helm install` would apply it. Imported by the tests that use them. Not a conftest.py, which
`from conftest import ...` elsewhere would find instead of tests/conftest.py.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MULTIPOD = REPO_ROOT / "airflow_automation" / "airflow_containers" / "multipod-helm"
POST_RENDER = MULTIPOD / "post-renderer" / "post-render.sh"


def chart_version():
    """The version of Airflow's chart that multipod-helm/Chart.yaml depends on."""
    chart = yaml.safe_load((MULTIPOD / "Chart.yaml").read_text(encoding="utf-8"))
    return next(d["version"] for d in chart["dependencies"] if d["name"] == "airflow")


@pytest.fixture(scope="session")
def airflow_chart(request):
    """Airflow's chart as a .tgz: $NGT_AIRFLOW_CHART, or pulled once into pytest's cache (300 KB from airflow.apache.org)."""
    if os.environ.get("NGT_AIRFLOW_CHART"):
        return Path(os.environ["NGT_AIRFLOW_CHART"])
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("needs helm")
    version = subprocess.run([helm, "version", "--template", "{{.Version}}"], capture_output=True, text=True).stdout
    found = re.match(r"v(\d+)\.(\d+)", version)
    if not found or tuple(map(int, found.groups())) < (3, 19):
        pytest.skip(f"needs helm 3.19 or later for Airflow's chart {chart_version()}, found {version!r}")
    cache = Path(request.config.cache.mkdir("airflow-chart"))
    chart = cache / f"airflow-{chart_version()}.tgz"
    if not chart.exists():
        pulled = subprocess.run(
            [helm, "pull", "airflow", "--repo", "https://airflow.apache.org", "--version", chart_version(), "-d", str(cache)],
            capture_output=True,
            text=True,
        )
        if pulled.returncode != 0:
            pytest.skip(f"couldn't download Airflow's chart: {pulled.stderr.strip()[:200]}")
    return chart


@pytest.fixture(scope="session")
def render_chart(airflow_chart, tmp_path_factory):
    """render_chart(namespace, *values_files): multipod-helm/ with its own values and these, through post-render.sh, as
    a list of dicts. From a copy, with Airflow's chart in its charts/, as `helm dependency build` would put it."""
    if sys.platform == "win32" or shutil.which("bash") is None or shutil.which("kubectl") is None:
        pytest.skip("the post-renderer needs bash and kubectl")
    chart = tmp_path_factory.mktemp("chart") / "multipod-helm"
    shutil.copytree(MULTIPOD, chart, ignore=shutil.ignore_patterns("charts"))
    (chart / "charts").mkdir()
    shutil.copy(airflow_chart, chart / "charts")

    def render(namespace, *values):
        args = ["helm", "template", "ngt", str(chart), "-n", namespace]
        for path in values:
            args += ["-f", str(path)]
        rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
        patched = subprocess.run(["bash", str(POST_RENDER)], input=rendered, capture_output=True, text=True, check=True)
        return [doc for doc in yaml.safe_load_all(patched.stdout) if doc]

    return render
