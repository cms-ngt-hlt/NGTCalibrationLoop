"""
Provenance through the fake toolchain: scenario-player/bin/{cmsDriver.py,cmsRun,uploadConditions.py}
run as real subprocesses -- the contract under test is theirs: the FAKE_INPUTS/FAKE_OUTPUTS markers
and the scope read off the cwd -- in the directories NGTLoopStep2/3/4.py use:

    <data>/<calibration>/run<N>/                     step2 (raw EOS -> .root)
    <data>/<calibration>/run<N>/alcaPromptJob<NNN>/  step3 (-> ALCARECO .root)
    <data>/<calibration>/run<N>/harvestJob<NNN>/     step4 (-> .db, then upload)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fake_conddb  # scenario-player/fake_conddb.py, on sys.path via tests/conftest.py

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_BIN = REPO_ROOT / "scenario-player" / "bin"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the fakes run via their shebangs and flock")


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """A $NGT_DEV_HOME with one run's step2 working dir."""
    monkeypatch.setenv("NGT_DEV_HOME", str(tmp_path))
    monkeypatch.delenv("NGT_CONDDB_DIR", raising=False)
    monkeypatch.delenv("NGT_FAULTS_DIR", raising=False)
    run_dir = tmp_path / "data" / "EcalPedestals" / "run398600"
    run_dir.mkdir(parents=True)
    return run_dir


def _run(script, args, cwd, ok=True):
    result = subprocess.run(
        [sys.executable, str(FAKE_BIN / script), *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    assert (result.returncode == 0) == ok, f"{script} exited {result.returncode}: {result.stdout}\n{result.stderr}"
    return result


def _cms_job(cwd, *, step_arg, inputs, python_filename, fileout=None):
    """One cmsDriver.py + cmsRun pair, the way every step's script does it."""
    args = ["expressStep", "-s", step_arg, "--filein", ",".join(inputs), "--python_filename", python_filename]
    if fileout:
        args += ["--fileout", f"file:{fileout}"]
    _run("cmsDriver.py", args, cwd)
    return _run("cmsRun", [python_filename], cwd)


def _seed_raw(run_dir, run, lumisections):
    """Raw "EOS" files, named the way seed.py names them."""
    paths = []
    for ls in lumisections:
        path = run_dir / f"run{run}_ls{ls:04d}.root"
        path.write_bytes(b"(FAKE) raw data placeholder\n")
        paths.append(f"root://eoscms.cern.ch/{path}")
    return paths


def _full_chain(run_dir, lumisections, run=398600):
    """step2 -> step3 -> step4 -> upload. Returns the harvest job dir."""
    step2_out = f"run{run}_LS{min(lumisections):04d}To{max(lumisections):04d}_ecalPedsStep2.root"
    raw = _seed_raw(run_dir, run, lumisections)
    _cms_job(run_dir, step_arg="RAW2DIGI,RECO", inputs=raw, python_filename="s2.py", fileout="tmp.root")
    (run_dir / "tmp.root").rename(run_dir / step2_out)

    alca_dir = run_dir / "alcaPromptJob000"
    alca_dir.mkdir()
    _cms_job(
        alca_dir,
        step_arg="ALCAOUTPUT:EcalTestPulsesRaw,ALCA:PromptCalibProdEcalPedestals",
        inputs=[f"file:../{step2_out}"],
        python_filename="s3.py",
    )

    harvest_dir = run_dir / "harvestJob000"
    harvest_dir.mkdir()
    _cms_job(
        harvest_dir,
        step_arg="ALCAHARVEST:EcalPedestals",
        inputs=["file:../alcaPromptJob000/PromptCalibProdEcalPedestals.root"],
        python_filename="s4.py",
    )
    (harvest_dir / "promptCalibConditions.db").rename(harvest_dir / "NGTCalibEcalPedestals.db")
    _run("uploadConditions.py", ["NGTCalibEcalPedestals.db"], harvest_dir)
    return harvest_dir


def _harvest_of_two_step3_outputs(run_dir, fed):
    """Two step3 jobs (LS 51-52 and 53-54), then a harvest fed the outputs of the jobs in `fed`.
    Returns the harvest's manifest."""
    for job_dir, lumisections in (("alcaPromptJob000", [51, 52]), ("alcaPromptJob001", [53, 54])):
        (run_dir / job_dir).mkdir()
        raw = _seed_raw(run_dir, 398600, lumisections)
        _cms_job(run_dir / job_dir, step_arg="ALCA:PromptCalibProdEcalPedestals", inputs=raw, python_filename="s3.py")
    harvest = run_dir / "harvestJob000"
    harvest.mkdir()
    inputs = [f"file:../{job_dir}/PromptCalibProdEcalPedestals.root" for job_dir in fed]
    _cms_job(harvest, step_arg="ALCAHARVEST:EcalPedestals", inputs=inputs, python_filename="s4.py")
    return fake_conddb.read_provenance(harvest / "promptCalibConditions.db")


# --- the generated config -------------------------------------------------------


def test_cmsdriver_records_inputs_and_strips_both_filein_prefixes(chain):
    _run(
        "cmsDriver.py",
        ["expressStep2", "-s", "RAW2DIGI,RECO", "--filein", "root://eoscms.cern.ch//eos/a.root,file:/data/b.root",
         "--fileout", "file:out.root", "--python_filename", "cfg.py"],
        chain,
    )

    config = (chain / "cfg.py").read_text(encoding="utf-8")
    assert "# FAKE_OUTPUTS=out.root" in config
    assert "# FAKE_INPUTS=/eos/a.root,/data/b.root" in config


def test_cmsdriver_still_names_the_fixed_harvest_output(chain):
    _run("cmsDriver.py", ["expressStep4", "-s", "ALCAHARVEST:EcalPedestals", "--python_filename", "cfg.py"], chain)
    assert "# FAKE_OUTPUTS=promptCalibConditions.db" in (chain / "cfg.py").read_text(encoding="utf-8")


# --- provenance propagation ------------------------------------------------------


def test_step2_derives_lumisections_from_the_raw_filenames(chain):
    raw = _seed_raw(chain, 398600, [51, 52])
    _cms_job(chain, step_arg="RAW2DIGI,RECO", inputs=raw, python_filename="s2.py", fileout="out.root")

    manifest = fake_conddb.read_provenance(chain / "out.root")
    assert (manifest["step"], manifest["calibration"], manifest["run"]) == ("step2", "EcalPedestals", 398600)
    assert manifest["lumisections"] == [51, 52]


@pytest.mark.parametrize("job_dir_name, step", [("alcaPromptJob000", "step3"), ("harvestJob001", "step4")])
def test_the_step_is_read_off_the_job_dir(chain, job_dir_name, step):
    job_dir = chain / job_dir_name
    job_dir.mkdir()
    raw = _seed_raw(chain, 398600, [51])
    _cms_job(job_dir, step_arg="RAW2DIGI,RECO", inputs=raw, python_filename="s.py", fileout="out.root")

    assert fake_conddb.read_provenance(job_dir / "out.root")["step"] == step


def test_lumisections_survive_all_three_steps(chain):
    harvest = _full_chain(chain, [51, 52, 53])

    manifest = fake_conddb.read_provenance(harvest / "NGTCalibEcalPedestals.db")
    assert (manifest["calibration"], manifest["run"]) == ("EcalPedestals", 398600)
    assert manifest["lumisections"] == [51, 52, 53]


def test_a_harvest_merges_the_lumisections_of_several_step3_outputs(chain):
    manifest = _harvest_of_two_step3_outputs(chain, ["alcaPromptJob000", "alcaPromptJob001"])
    assert manifest["lumisections"] == [51, 52, 53, 54]
    assert len(manifest["inputs"]) == 2


def test_a_harvest_fed_the_wrong_inputs_produces_a_wrong_payload(chain):
    """Provenance comes from the inputs actually passed: a harvest missing one is visibly short."""
    assert _harvest_of_two_step3_outputs(chain, ["alcaPromptJob000"])["lumisections"] == [51, 52]


# --- the upload ------------------------------------------------------------------


def test_upload_appends_one_row_per_successful_upload(chain):
    harvest = _full_chain(chain, [51])
    _run("uploadConditions.py", ["NGTCalibEcalPedestals.db"], harvest)

    rows = fake_conddb.read_ledger()
    assert [row["seq"] for row in rows] == [1, 2]
    assert {(row["calibration"], row["run"], tuple(row["lumisections"]), row["db_file"]) for row in rows} == {
        ("EcalPedestals", 398600, (51,), "NGTCalibEcalPedestals.db")
    }


def test_upload_does_not_read_the_metadata_txt_step4_writes(chain):
    """The ledger records what the pipeline processed: a metadata file naming another run stays out."""
    harvest = _full_chain(chain, [51, 52])
    (harvest / "NGTCalibEcalPedestals.txt").write_text(
        json.dumps({"since": 999999, "destinationTags": {"SomeOtherTag": {}}}), encoding="utf-8"
    )

    _run("uploadConditions.py", ["NGTCalibEcalPedestals.db"], harvest)

    latest = fake_conddb.read_ledger()[-1]
    assert latest["run"] == 398600
    assert "since" not in latest and "tag" not in latest


@pytest.mark.parametrize("content, reason", [(b"not one of ours\n", "no provenance manifest"), (None, "no such file")])
def test_upload_records_nothing_for_a_db_it_cannot_read(chain, content, reason):
    """Not even an empty payload: HARVESTING.sh uploads after a failed cmsRun too, with no .db."""
    harvest = chain / "harvestJob000"
    harvest.mkdir()
    if content is not None:
        (harvest / "NGTCalibEcalPedestals.db").write_bytes(content)

    result = _run("uploadConditions.py", ["NGTCalibEcalPedestals.db"], harvest, ok=False)

    assert reason in result.stderr
    assert fake_conddb.read_ledger() == []


def test_upload_writes_no_row_when_a_fault_is_armed(chain, tmp_path, monkeypatch):
    """"Nothing should reach the conditions DB" is only assertable if a failed upload writes no row."""
    harvest = _full_chain(chain, [51])
    faults_dir = tmp_path / "faults"
    faults_dir.mkdir()
    (faults_dir / "upload_conditions.json").write_text(
        json.dumps([{"mode": "exit_code", "exit_code": 1, "calibration": "EcalPedestals", "times_remaining": 1}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("NGT_FAULTS_DIR", str(faults_dir))

    result = _run("uploadConditions.py", ["NGTCalibEcalPedestals.db"], harvest, ok=False)

    assert result.returncode == 1 and "(FAKE) injected upload_conditions fault" in result.stderr
    assert len(fake_conddb.read_ledger()) == 1  # only _full_chain's own upload
