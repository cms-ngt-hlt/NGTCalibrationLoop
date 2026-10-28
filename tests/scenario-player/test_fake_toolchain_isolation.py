"""
Tests that sim_env.sh's generated env.sh makes the fake CMSSW toolchain win over
a cmsenv exported by the operator's shell. Unlike the rest of the suite these
spawn a real bash, since what is under test is bash's own name resolution.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="needs a POSIX bash"
)

# The line env.sh carries (see sim_setup in scenario-player/sim_env.sh).
UNSET_LINE = "unset -f cmsenv cmsrel cmsDriver.py cmsRun uploadConditions.py 2>/dev/null || true"


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cmsenv = bin_dir / "cmsenv"
    cmsenv.write_text("#!/bin/bash\necho '(FAKE) cmsenv: no-op'\n", encoding="utf-8")
    cmsenv.chmod(0o755)
    return bin_dir


def _run_job_with_decoy(tmp_path, fake_bin, env_sh_body):
    """Run a job script whose cmsenv is a bare word -- exactly as
    NGTLoopStep2/3/4.py write it -- from a shell that exported a decoy cmsenv,
    after sourcing env_sh_body. Returns (stdout, decoy_ran)."""
    decoy_marker = tmp_path / "decoy_ran.txt"
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(env_sh_body, encoding="utf-8")

    job = tmp_path / "job.sh"
    job.write_text("#!/bin/bash -ex\ncmsenv\n", encoding="utf-8")

    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        f"""#!/bin/bash
cmsenv() {{ echo decoy >> "{decoy_marker}"; echo "DECOY cmsenv RAN"; }}
export -f cmsenv
source "{env_sh}"
bash "{job}"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True, check=False
    )
    return result.stdout, decoy_marker.exists()


def test_env_sh_makes_the_fake_toolchain_win(tmp_path, fake_bin):
    stdout, decoy_ran = _run_job_with_decoy(
        tmp_path, fake_bin, f'export PATH="{fake_bin}:$PATH"\n{UNSET_LINE}\n'
    )
    assert "(FAKE) cmsenv: no-op" in stdout
    assert not decoy_ran


def test_path_alone_does_not_beat_an_exported_function(tmp_path, fake_bin):
    """Without the unset, the decoy wins even with the fake first on $PATH --
    so the test above is proving the unset line, not just PATH order."""
    stdout, decoy_ran = _run_job_with_decoy(
        tmp_path, fake_bin, f'export PATH="{fake_bin}:$PATH"\n'
    )
    assert "(FAKE) cmsenv: no-op" not in stdout
    assert decoy_ran


def test_sim_env_generates_the_unset_line():
    """Ties UNSET_LINE above to what sim_setup actually writes, so changing one
    without the other fails instead of leaving the tests passing on a line the
    real harness no longer emits."""
    sim_env = Path(__file__).resolve().parents[2] / "scenario-player" / "sim_env.sh"
    assert UNSET_LINE in sim_env.read_text(encoding="utf-8")
