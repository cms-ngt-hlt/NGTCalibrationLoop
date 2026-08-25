"""Step 3 (RECO -> ALCARECO) processing logic, ported from NGTLoopStep3.py's FSM.

Stateless, same rationale as step2.py. One additional change beyond the
incremental-processed-log pattern: ALCA job directories are named from a hash
of their input file set instead of a monotonically incrementing in-memory
counter (`self.alcaJobNumber`), because Airflow retries a failed task by
re-executing the same callable -- a counter would mint a new (duplicate) job
directory on every retry, where a content hash reproduces the same one.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, shell

MINIMUM_FILES_PER_BATCH = 1
TIMEOUT_SECONDS = 9 * 60 * 60  # kept as-is from NGTLoopStep3.py (comment there says "8 hours"; value is 9h)

PROCESSED_LOG_NAME = "allStep2FilesProcessed.log"
RUN_END_LOG_NAME = "runEnd.log"
RUN_START_LOG_NAME = "runStart.log"


@dataclass
class RunContext:
    calibration_name: str
    run_number: str
    start_time: datetime
    working_dir: Path
    calib_config: dict
    ngt_params: dict


@dataclass
class CycleDecision:
    action: str  # "batch" | "final" | "wait"
    files_to_process: set


@dataclass
class JobSpec:
    job_dir: Path
    script_name: str
    witness_file: str
    input_files: set


def _path_where_files_appear(ngt_params, calibration_name):
    data_base_path = ngt_params.get("DATA_BASE_PATH", "/data/ngt")
    return os.path.join(data_base_path, calibration_name) + "/"


def find_new_run(calibration_name, already_latched_run_numbers):
    """Scan for a run directory not already latched by a processor DAG chain
    (the caller supplies that set, replacing the old in-memory
    setOfRunsProcessed). Returns the earliest new run's number as a string, or
    None."""
    ngt_params = config.load_ngt_parameters()
    path = Path(_path_where_files_appear(ngt_params, calibration_name))
    if not path.exists():
        return None

    current_dirs = {p.name for p in path.iterdir() if p.is_dir()}
    already = {f"run{n}" for n in already_latched_run_numbers}
    new_runs = {p for p in (current_dirs - already) if p.startswith("run")}
    if not new_runs:
        return None

    return sorted(new_runs)[0][3:]


def build_run_context(calibration_name, run_number):
    ngt_params = config.load_ngt_parameters()
    calib_config = config.load_calibration_config(calibration_name)
    working_dir = Path(_path_where_files_appear(ngt_params, calibration_name)) / f"run{run_number}"

    start_log = working_dir / RUN_START_LOG_NAME
    if start_log.exists():
        start_time = datetime.fromisoformat(start_log.read_text(encoding="utf-8").strip())
    else:
        # Weird, how come we don't have a runStart.log? Fall back to now, and
        # persist it so later cycles (fresh DAG runs) see a stable value
        # instead of drifting "now" forward every cycle.
        logging.info("We didn't find a runStart.log file... setting run start to NOW")
        start_time = datetime.now(timezone.utc)
        start_log.write_text(start_time.isoformat(), encoding="utf-8")

    return RunContext(
        calibration_name=calibration_name,
        run_number=run_number,
        start_time=start_time,
        working_dir=working_dir,
        calib_config=calib_config,
        ngt_params=ngt_params,
    )


def _run_is_not_complete(working_dir):
    return not (Path(working_dir) / RUN_END_LOG_NAME).exists()


def _still_have_time(start_time):
    diff = datetime.now(timezone.utc) - start_time
    return diff.total_seconds() <= TIMEOUT_SECONDS


def load_already_processed(working_dir):
    log_path = Path(working_dir) / PROCESSED_LOG_NAME
    if not log_path.exists():
        return set()
    return {line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _available_files(ctx: RunContext):
    conf = ctx.calib_config["step_3_config"]
    suffix_control = conf["step_2_witness_suffix"]
    root_suffix = conf["step_2_root_suffix"]

    control_files = {str(p) for p in Path(ctx.working_dir).glob(f"run*{suffix_control}")}
    changed = {
        (s[: -len(suffix_control)] + root_suffix if s.endswith(suffix_control) else s)
        for s in control_files
    }
    return {Path(s) for s in changed}


def check_files_for_processing(ctx: RunContext):
    """Mirrors CheckFilesForProcessing + the ContinueAfterCheckFiles transition
    conditions, in the same precedence order as the original transition list:
    enough files waiting takes priority even over an expired timeout (unlike
    Step 2, where an expired timeout wins outright)."""
    already_processed = {Path(p) for p in load_already_processed(ctx.working_dir)}
    available = _available_files(ctx)
    files_to_process = available - already_processed

    enough_files = len(files_to_process) >= MINIMUM_FILES_PER_BATCH

    if files_to_process and enough_files:
        return CycleDecision(action="batch", files_to_process=files_to_process)

    if _run_is_not_complete(ctx.working_dir) and _still_have_time(ctx.start_time):
        return CycleDecision(action="wait", files_to_process=files_to_process)

    return CycleDecision(action="final", files_to_process=files_to_process)


def _job_dir_name(input_files):
    key = "|".join(sorted(str(p) for p in input_files))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"alcaPromptJob_{digest}"


def prepare_alca_prompt_job(ctx: RunContext, files_to_process) -> Optional[JobSpec]:
    """Validate pending files still exist (a witness file can appear before its
    root file is fully flushed) and, if any survive, write the ALCAOUTPUT.sh
    cmsDriver script. Returns None if nothing to process."""
    input_files = {f for f in files_to_process if Path(f).exists()}
    if not input_files:
        return None

    job_dir = Path(ctx.working_dir) / _job_dir_name(input_files)
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o777)

    conf = ctx.calib_config["step_3_config"]["cms_driver"]
    python_filename = f"run{ctx.run_number}{conf['python_filename_affix']}.py"
    witness_file = conf["step_3_witness_suffix"]
    log_filename = python_filename.replace(".py", ".log")

    filein_paths = ",".join("file:" + str(p) for p in input_files)

    python_config_mods = ""
    if conf.get("python_config_mods"):
        mods = "\n".join(conf["python_config_mods"])
        python_config_mods = f"cat <<@EOF>> {python_filename}\n{mods}\n@EOF\n"

    rm_express_files = "\n".join(f"  rm {p}" for p in input_files)

    ngt_params = ctx.ngt_params
    alca_job_file = job_dir / "ALCAOUTPUT.sh"
    with alca_job_file.open("w", encoding="utf-8") as f:
        f.write(
            f"""#!/bin/bash -ex

export $SCRAM_ARCH={ngt_params["SCRAM_ARCH"]}
cd {ctx.working_dir}/{ngt_params["CMSSW_VERSION"]}/src
cmsenv
cd -

cmsDriver.py expressStep3 --conditions {ngt_params["GLOBAL_TAG"]} \\
-s {conf["step"]} --datatier ALCARECO --eventcontent ALCARECO \\
--triggerResultsProcess RERECO --nThreads 8 --nStreams 8 -n -1 \\
--filein {filein_paths} --no_exec --python_filename {python_filename}

{python_config_mods}
if cmsRun {python_filename} > {log_filename} 2>&1; then
  touch {witness_file}
  # Step 3 succeeded, now deleting Step 2 input files
{rm_express_files}
else
  echo 'cmsRun failed' >> {log_filename}
  exit 1
fi
"""
        )

    return JobSpec(job_dir=job_dir, script_name="ALCAOUTPUT.sh", witness_file=witness_file, input_files=input_files)


def run_alca_prompt_job(job_spec: JobSpec):
    """Run the prepared ALCAOUTPUT.sh script to completion (blocking). Raises
    shell.JobScriptFailedError on failure so the Airflow task retries."""
    return shell.run_job_script(
        job_spec.script_name, job_spec.job_dir, stdout_log="stdout.log", stderr_log="stderr.log"
    )


def finalize_cycle(ctx: RunContext, job_spec: Optional[JobSpec], decision: CycleDecision):
    """Durably record a successfully-launched batch. The same log this appends
    to (allStep2FilesProcessed.log) is also the "processed so far" summary the
    original FSM only wrote once, at final cleanup -- here it's simply
    complete-by-construction once the final cycle appends its own batch."""
    if job_spec is not None:
        log_path = Path(ctx.working_dir) / PROCESSED_LOG_NAME
        with open(log_path, "a", encoding="utf-8") as f:
            for file_path in sorted(job_spec.input_files, key=str):
                f.write(str(file_path) + "\n")

    return decision.action == "final"
