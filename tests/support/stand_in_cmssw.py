"""A stand-in CMSSW on PATH, for tests that run a generated job script for real with bash: `cmsenv` and
`cmsDriver.py` do nothing, `cmsRun` runs the bash the test gives it in the job's directory, and `uploadConditions.py`
only notes its arguments in uploaded.txt there."""

import os
import stat


def install(bin_dir, monkeypatch, cms_run="true"):
    bin_dir.mkdir(parents=True, exist_ok=True)
    scripts = {"cmsenv": "true", "cmsDriver.py": "true", "cmsRun": cms_run, "uploadConditions.py": 'echo "$@" >> uploaded.txt'}
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


# cmsRun bodies: what a harvest can leave.
PAYLOAD = "echo payload > promptCalibConditions.db"
EMPTY_PAYLOAD = ": > promptCalibConditions.db"  # a harvester with too few entries for its thresholds
NO_PAYLOAD_FILE = "true"
FAILS = "exit 65"
