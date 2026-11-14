"""One file through steps 2, 3 and 4 as the DAGs run them (ngt_calibration_loop.run_step), with every job script run for
real, in the scenario player's scratch environment (sim_env.sh's sim_setup) and against its dummy conditions DB.

- By default, on its fake toolchain: a placeholder for LS 51-54, in seconds.
- With `pytest -m cmssw`, on a real file with real CMSSW: the real-cmssw scenario's run, lumisections and
  calibration_config, on the file NGT_TEST_INPUT names. Run it in a shell where cmsset_default.sh was sourced and a
  release cmsenv'd, whose Python has pytest and PyYAML, and which reaches Frontier: lxplus, a GitLab runner with CVMFS,
  or a CMSSW pod. The steps run in that release. Steps 2 and 3 take about three minutes each on run 403634's file:

      NGT_TEST_INPUT=/path/to/370f0750-b4e9-4d43-a0f5-920069a891e0.root pytest -m cmssw

Either way, the payload's row in the dummy conditions DB has to cover the file's lumisections, from the library's lineage
for a real payload, and the upload is always the fake one: ngtParameters.jsn names it by path.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import omsapi
import pytest
import yaml

import fake_conddb  # scenario-player/fake_conddb.py
import seed  # scenario-player/seed.py
from ngt_calibration_loop import lineage, run_step, step2

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYER = REPO_ROOT / "scenario-player"
REAL_SCENARIO = PLAYER / "scenarios" / "real-cmssw" / "ecal_pedestals_403634.yaml"
CALIBRATION = "EcalPedestals"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the job scripts with bash")


def _scratch_environment(tmp_path, monkeypatch):
    """sim_setup's scratch environment, which this process's library and the job scripts then use."""
    home = tmp_path / "demo-env"
    env = dict(os.environ, NGT_DEV_HOME=str(home), NGT_SIM_STUB_INSTALLED="1")
    subprocess.run(["bash", "-c", f'source "{PLAYER / "sim_env.sh"}" && sim_setup > /dev/null'], env=env, check=True)
    for name, value in (
        ("NGT_DEV_HOME", home),
        ("NGT_PARAMETERS_PATH", home / "ngtParameters.jsn"),
        ("NGT_CALIBRATION_YAML_DIR", home / "calibrationYAML"),
    ):
        monkeypatch.setenv(name, str(value))
    return home


def _seed(home, run, ls, source=None):
    """The run, live in OMS as the library sees it, and its one file in the fake EOS. Returns the file's path."""
    seed_py = [sys.executable, str(PLAYER / "seed.py")]
    env = dict(os.environ, NGT_DEV_HOME=str(home))
    subprocess.run([*seed_py, "seed-run", "--calibration", CALIBRATION, "--run", str(run)], env=env, check=True, capture_output=True)
    add = [*seed_py, "add-ls", "--calibration", CALIBRATION, "--run", str(run), "--ls", ls, *(["--file", source] if source else [])]
    added = subprocess.run(add, env=env, check=True, capture_output=True, text=True).stdout
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([{
        "run_number": run, "fill_type_runtime": "PROTONS", "l1_hlt_mode": "collisions2026", "stable_beam": True,
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_time": None, "last_lumisection_number": 2000,
    }])
    assert step2.find_new_run(CALIBRATION) == run
    return added.strip().rsplit(": ", 1)[1]


def _through_every_step(run, file_path):
    for step in ("step2_express", "step3_alca", "step4_harvest", "step4_upload"):
        result = run_step.STEPS[step](CALIBRATION, run, file_path)
        assert result, f"{step} recorded nothing"
    return result["payload"]


def test_one_file_through_every_step_on_the_fake_toolchain(tmp_path, monkeypatch):
    home = _scratch_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{PLAYER / 'bin'}{os.pathsep}{os.environ['PATH']}")
    for name in [n for n in os.environ if n.startswith("BASH_FUNC_")]:  # a real cmsenv's, which bash would prefer
        monkeypatch.delenv(name)
    file_path = _seed(home, 398601, "51-54")

    payload = _through_every_step(398601, file_path)

    (row,) = fake_conddb.read_ledger()
    assert (row["calibration"], row["run"], row["lumisections"]) == (CALIBRATION, 398601, [51, 52, 53, 54])
    assert lineage.read(payload)["lumisections"] == row["lumisections"], "the library's lineage agrees with the fakes"


@pytest.mark.cmssw
def test_a_real_file_through_every_step_with_real_cmssw(tmp_path, monkeypatch):
    source = os.environ.get("NGT_TEST_INPUT")
    if not source or not Path(source).is_file():
        pytest.skip("NGT_TEST_INPUT names no file: the real RAW file the real-cmssw scenario plays")
    if not os.environ.get("CMSSW_BASE") or "BASH_FUNC_cmsenv%%" not in os.environ:
        pytest.skip("needs a shell where cmsset_default.sh was sourced and a release cmsenv'd")
    scenario = yaml.safe_load(REAL_SCENARIO.read_text(encoding="utf-8"))
    (spec,) = scenario["runs"]
    (entry,) = spec["lumisections"]
    home = _scratch_environment(tmp_path, monkeypatch)
    # The steps run in this shell's release: its area is the one every step's jobs cmsenv in.
    params_path = home / "ngtParameters.jsn"
    params = json.loads(params_path.read_text(encoding="utf-8"))
    cmssw_base = Path(os.environ["CMSSW_BASE"])
    params.update(CMSSW_AREA=str(cmssw_base.parent), CMSSW_VERSION=cmssw_base.name, SCRAM_ARCH=os.environ["SCRAM_ARCH"])
    params_path.write_text(json.dumps(params, indent=4), encoding="utf-8")
    monkeypatch.setattr(seed, "CALIB_YAML_DIR", home / "calibrationYAML")
    seed.apply_calibration_config(scenario.get("calibration_config") or {})
    file_path = _seed(home, spec["run"], str(entry["ls"]), source)

    payload = _through_every_step(spec["run"], file_path)

    (row,) = fake_conddb.read_ledger()
    first, last = (int(ls) for ls in str(entry["ls"]).split("-"))
    assert (row["calibration"], row["run"], row["payload"]) == (CALIBRATION, spec["run"], "real")
    assert row["payload_bytes"] > 0 and row["lumisections"] == list(range(first, last + 1))
    assert Path(payload).is_file()
