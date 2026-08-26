"""
Shared pytest fixtures for the NGTCalibrationLoop test suite.

These tests exercise the *logic* of the three FSM loop scripts (run/file latching,
LS/file batching, timeouts, cleanup) with every external dependency mocked out:

- OMS REST API -> tests/stubs/omsapi (in-memory fake, see that module's docstring)
- edmFileUtil / xrdfs (EOS access)  -> tests/support/fake_subprocess.FakeEOS
- cmsDriver.py / cmsRun / uploadConditions.py -> never actually invoked;
  ngt_calibration_loop.shell.run_job_script is replaced with a recorder (see the
  `job_runner` fixture) so "launching a job" just records what would have run
- /data/ngt, /tmp/ngt, /nfshome0/sakura -> redirected to a pytest tmp_path via the
  NGT_PARAMETERS_PATH / NGT_CALIBRATION_YAML_DIR env var overrides read by the
  NGTLoopStepN.py scripts (see `_load_ngt_parameters` / `_load_calibration_config`
  in each script)

No network access, no CMSSW, no real filesystem paths outside of pytest's tmp_path
are required to run this suite.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
STUBS_DIR = TESTS_DIR / "stubs"

# Order matters: the stub omsapi must be found before any real one, and the repo
# root must be importable so `import NGTLoopStep2` etc. works regardless of the
# directory pytest was invoked from.
for path in (str(TESTS_DIR), str(STUBS_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import omsapi  # noqa: E402  (test stub, see tests/stubs/omsapi/__init__.py)


@pytest.fixture(autouse=True)
def _reset_oms_stub():
    """Ensure no OMS run data/failure mode leaks between tests."""
    omsapi.reset()
    yield
    omsapi.reset()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Redirect all filesystem paths the loop scripts use into tmp_path.

    Returns a namespace with the resolved directories so tests can seed input
    files (e.g. runStart.log, witness files) and assert on output files.
    """
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    cond_auth_dir = tmp_path / "cond_auth"
    calib_yaml_dir = tmp_path / "calibrationYAML"
    for d in (data_dir, log_dir, cond_auth_dir, calib_yaml_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Use the real, checked-in calibration YAMLs so tests exercise real config,
    # not a hand-rolled fixture that could drift from production.
    real_calib_dir = REPO_ROOT / "calibrationYAML"
    for yaml_file in real_calib_dir.glob("*.yaml"):
        (calib_yaml_dir / yaml_file.name).write_text(
            yaml_file.read_text(encoding="utf-8"), encoding="utf-8"
        )

    parameters_path = tmp_path / "ngtParameters.jsn"
    parameters_path.write_text(
        json.dumps(
            {
                "SCRAM_ARCH": "el8_amd64_gcc13",
                "CMSSW_VERSION": "CMSSW_16_0_7_patch1",
                "GLOBAL_TAG": "160X_dataRun3_ExpressNGT_v0",
                "DATA_BASE_PATH": str(data_dir),
                "LOG_BASE_PATH": str(log_dir),
                "COND_AUTH_PATH": str(cond_auth_dir),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("NGT_PARAMETERS_PATH", str(parameters_path))
    monkeypatch.setenv("NGT_CALIBRATION_YAML_DIR", str(calib_yaml_dir))

    return SimpleNamespace(
        tmp_path=tmp_path,
        data_dir=data_dir,
        log_dir=log_dir,
        cond_auth_dir=cond_auth_dir,
        calib_yaml_dir=calib_yaml_dir,
        parameters_path=parameters_path,
    )


@pytest.fixture
def popen_calls(monkeypatch):
    """Replace subprocess.Popen everywhere with a recorder, so tests never try to
    actually launch cmsDriver/cmsRun/uploadConditions.py. Returns the list of
    recorded calls (each a dict with "cmd" and "kwargs")."""
    import subprocess

    from support.fake_subprocess import FakePopen

    calls = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen(calls))
    return calls


@pytest.fixture
def job_runner(monkeypatch):
    """Replace ngt_calibration_loop.shell.run_job_script with a recorder, so
    tests never try to actually launch cmsDriver/cmsRun/uploadConditions.py.

    Returns a list-like recorder of calls (each a dict with script_name/cwd/
    stdout_log/stderr_log). Succeeds by default; call
    `job_runner.queue_failure()` before an action to make the *next* launch
    raise shell.JobScriptFailedError, for exercising Airflow's retry path.
    """
    from ngt_calibration_loop import shell

    class JobRunnerRecorder(list):
        def __init__(self):
            super().__init__()
            self._outcomes = []

        def queue_failure(self, exc=None):
            self._outcomes.append(exc or shell.JobScriptFailedError("forced test failure"))

    recorder = JobRunnerRecorder()

    def fake_run_job_script(script_name, cwd, stdout_log=None, stderr_log=None, timeout=None):
        recorder.append(
            {"script_name": script_name, "cwd": str(cwd), "stdout_log": stdout_log, "stderr_log": stderr_log}
        )
        if recorder._outcomes:
            raise recorder._outcomes.pop(0)
        return shell.JobResult(returncode=0, stdout_log=stdout_log, stderr_log=stderr_log)

    monkeypatch.setattr(shell, "run_job_script", fake_run_job_script)
    return recorder


@pytest.fixture
def fake_eos(monkeypatch):
    """Replace subprocess.run everywhere with a FakeEOS-backed responder for
    edmFileUtil / xrdfs calls (only used by Step 2). Returns the FakeEOS so tests
    can seed which files "exist" on EOS."""
    import subprocess

    from support.fake_subprocess import FakeEOS, make_fake_run

    eos = FakeEOS()
    monkeypatch.setattr(subprocess, "run", make_fake_run(eos))
    return eos
