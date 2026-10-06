"""Blocking job-script execution -- the seam that replaces the old
subprocess.Popen(...) "launch and forget" calls in NGTLoopStep2/3/4.py.

The FSM scripts used Popen specifically because they couldn't afford to block
their own polling loop while a cmsRun job ran. An Airflow task already runs in
its own worker slot, so it can simply block on the job and let Airflow's own
retry/timeout/alerting machinery observe real success or failure -- which is
the whole point of this migration. Tests replace run_job_script itself (see
tests/conftest.py's job_runner fixture) with a controllable
success/failure/exception double, so retry behavior is directly testable.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional


class JobScriptFailedError(RuntimeError):
    """Raised when a launched job script exits non-zero, so the calling
    Airflow task fails (and is retried per its own retry policy) instead of
    silently continuing as the original Popen-based code did."""


@dataclass
class JobResult:
    returncode: int
    stdout_log: Optional[str]
    stderr_log: Optional[str]


def run_job_script(script_name, cwd, stdout_log=None, stderr_log=None, timeout=None):
    """Run `bash <script_name>` in `cwd` to completion.

    If stdout_log/stderr_log are given (relative to cwd), the process's output
    is captured there (matching Step 3/4's stdout.log/stderr.log convention);
    otherwise it's discarded (matching Step 2, whose script redirects cmsRun's
    own output internally). Raises JobScriptFailedError on non-zero exit.
    """
    cwd = str(cwd)
    logging.info(f"Running job script {script_name} in {cwd}")

    stdout_handle = (
        open(os.path.join(cwd, stdout_log), "w", encoding="utf-8") if stdout_log else subprocess.DEVNULL
    )
    stderr_handle = (
        open(os.path.join(cwd, stderr_log), "w", encoding="utf-8") if stderr_log else subprocess.DEVNULL
    )
    try:
        result = subprocess.run(
            ["bash", script_name],
            cwd=cwd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            timeout=timeout,
            check=False,
        )
    finally:
        if stdout_handle is not subprocess.DEVNULL:
            stdout_handle.close()
        if stderr_handle is not subprocess.DEVNULL:
            stderr_handle.close()

    if result.returncode != 0:
        raise JobScriptFailedError(
            f"{script_name} in {cwd} exited with code {result.returncode}"
            f" (see {stdout_log or 'stdout'}/{stderr_log or 'stderr'})"
        )

    logging.info(f"Job script {script_name} in {cwd} completed successfully")
    return JobResult(returncode=result.returncode, stdout_log=stdout_log, stderr_log=stderr_log)
