"""Step 4 (ALCARECO -> Conditions) processing logic, ported from
NGTLoopStep4.py's FSM.

Two changes beyond the general stateless/idempotent-naming pattern shared
with step3.py:

- The harvesting cmsRun job and the uploadConditions.py call used to be one
  bash script launched by one Popen. They're now two separate functions
  (run_harvesting_job / upload_conditions), meant to become two separate
  Airflow tasks with independent retry policies -- a transient condDB upload
  failure should retry on its own without re-running ALCAHARVEST.
- "Already processed" here does NOT gate what gets harvested (Step 4
  deliberately re-harvests *everything* available, every cycle -- more
  statistics improve the payload even for already-uploaded conditions, see
  NGTLoopStep4.py's CheckFilesForProcessing). It only gates whether there's
  anything *new* worth triggering another harvest cycle over (`waiting`
  below), so the processed-log is overwritten with the full current set each
  cycle rather than appended to.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, shell

MINIMUM_FILES_PER_BATCH = 1
# Overridable per calibration (step_4_config.timeoutSeconds) -- see
# step2.py's DEFAULT_MAX_LATCH_TIME_HOURS for why a live-test setup needs that.
DEFAULT_TIMEOUT_SECONDS = 8 * 60 * 60
TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS  # kept for backward-compat imports/tests

PROCESSED_LOG_NAME = "allStep3FilesProcessed.log"
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
    cmssw_path: str
    cond_auth_path: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass
class CycleDecision:
    action: str  # "batch" | "final" | "wait"
    files_to_process: set


@dataclass
class JobSpec:
    job_dir: Path
    script_name: str
    final_db_name: str
    metadata_filename: str
    input_files: set


def _path_where_files_appear(ngt_params, calibration_name):
    data_base_path = ngt_params.get("DATA_BASE_PATH", "/data/ngt")
    return os.path.join(data_base_path, calibration_name) + "/"


def find_new_run(calibration_name, already_latched_run_numbers):
    """Same run-directory-scan/dedup pattern as step3.find_new_run."""
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
        logging.info("We didn't find a runStart.log file... setting run start to NOW")
        start_time = datetime.now(timezone.utc)
        start_log.write_text(start_time.isoformat(), encoding="utf-8")

    cmssw_path = calib_config["step_4_config"]["cmssw_base_path"]
    cond_auth_path = os.path.expanduser(ngt_params.get("COND_AUTH_PATH", "/nfshome0/sakura"))
    os.environ["COND_AUTH_PATH"] = cond_auth_path

    return RunContext(
        calibration_name=calibration_name,
        run_number=run_number,
        start_time=start_time,
        working_dir=working_dir,
        calib_config=calib_config,
        ngt_params=ngt_params,
        cmssw_path=cmssw_path,
        cond_auth_path=cond_auth_path,
        timeout_seconds=calib_config["step_4_config"].get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS),
    )


def _run_is_not_complete(working_dir):
    return not (Path(working_dir) / RUN_END_LOG_NAME).exists()


def _still_have_time(start_time, timeout_seconds):
    diff = datetime.now(timezone.utc) - start_time
    return diff.total_seconds() <= timeout_seconds


def load_already_processed(working_dir):
    log_path = Path(working_dir) / PROCESSED_LOG_NAME
    if not log_path.exists():
        return set()
    return {line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _available_files(ctx: RunContext):
    conf = ctx.calib_config["step_4_config"]
    control_name = conf["step_3_witness_suffix"]
    target_name = conf["step_3_root_filename"]

    control_files = {str(p) for p in Path(ctx.working_dir).rglob(control_name)}
    changed = {
        (s[: -len(control_name)] + target_name if s.endswith(control_name) else s)
        for s in control_files
    }
    return {Path(s) for s in changed}


def check_files_for_processing(ctx: RunContext):
    """Mirrors CheckFilesForProcessing's "reprocess everything" quirk: whether
    there's something new (`waiting`) is judged against the incremental
    delta, but the batch that actually gets (re-)harvested is always the full
    available set, and `enough files` is judged against that full set too."""
    already_processed = {Path(p) for p in load_already_processed(ctx.working_dir)}
    available = _available_files(ctx)
    new_files = available - already_processed
    waiting = len(new_files) > 0

    files_to_process = available
    enough_files = len(files_to_process) >= MINIMUM_FILES_PER_BATCH

    if waiting and files_to_process and enough_files:
        return CycleDecision(action="batch", files_to_process=files_to_process)

    if _run_is_not_complete(ctx.working_dir) and _still_have_time(ctx.start_time, ctx.timeout_seconds):
        return CycleDecision(action="wait", files_to_process=files_to_process)

    return CycleDecision(action="final", files_to_process=files_to_process)


def _job_dir_name(input_files):
    key = "|".join(sorted(str(p) for p in input_files))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"harvestJob_{digest}"


def prepare_harvesting_job(ctx: RunContext, files_to_process) -> Optional[JobSpec]:
    """Validate pending files still exist and, if any survive, write the
    upload metadata JSON and the HARVESTING.sh cmsDriver script (cmsRun
    ALCAHARVEST only -- the upload is a separate step, see upload_conditions).
    Returns None if nothing to process."""
    input_files = {f for f in files_to_process if Path(f).exists()}
    if not input_files:
        return None

    job_dir = Path(ctx.working_dir) / _job_dir_name(input_files)
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o777)

    conf_step4 = ctx.calib_config["step_4_config"]
    conf_driver = conf_step4["cms_driver"]
    conf_upload = conf_step4["upload_metadata"]

    since = None if "BeamSpot" in ctx.calibration_name else ctx.run_number

    metadata = {
        "destinationDatabase": conf_upload["destinationDatabase"],
        "destinationTags": conf_upload["destinationTags"],
        "inputTag": conf_upload["inputTag"],
        "since": since,
        "userText": conf_upload["userText"],
    }
    metadata_filename = conf_step4["metadata_filename"]
    (job_dir / metadata_filename).write_text(json.dumps(metadata, indent=4), encoding="utf-8")

    python_filename = f"run{ctx.run_number}{conf_driver['python_filename_affix']}.py"
    filein_paths = ",".join("file:" + str(p) for p in input_files)
    python_config_mods = "\n".join(conf_driver["python_config_mods"])
    final_db_name = conf_step4["final_db_name"]

    ngt_params = ctx.ngt_params
    harvesting_script = job_dir / "HARVESTING.sh"
    with harvesting_script.open("w", encoding="utf-8") as f:
        f.write(
            f"""#!/bin/bash -ex

export $SCRAM_ARCH={ngt_params["SCRAM_ARCH"]}
cd {ctx.cmssw_path}/{ngt_params["CMSSW_VERSION"]}/src
cmsenv
cd -

cmsDriver.py expressStep4 --conditions {ngt_params["GLOBAL_TAG"]} \\
-s {conf_driver["step"]} --scenario {conf_driver["scenario"]} --data \\
--filein {filein_paths} -n -1 --no_exec --python_filename {python_filename}

cat <<@EOF>> {python_filename}
{python_config_mods}
@EOF

cmsRun {python_filename}

if [ -f "promptCalibConditions.db" ]; then echo "DB file exists!"; else echo "DB file missing"; fi
mv promptCalibConditions.db {final_db_name}
if [ -f "{metadata_filename}" ]; then echo "Metadata file exists!"; else echo "Metadata file missing"; fi
"""
        )

    return JobSpec(
        job_dir=job_dir,
        script_name="HARVESTING.sh",
        final_db_name=final_db_name,
        metadata_filename=metadata_filename,
        input_files=input_files,
    )


def run_harvesting_job(job_spec: JobSpec):
    """Run HARVESTING.sh (ALCAHARVEST only) to completion. Raises
    shell.JobScriptFailedError on failure so the Airflow task retries."""
    return shell.run_job_script(
        job_spec.script_name, job_spec.job_dir, stdout_log="stdout.log", stderr_log="stderr.log"
    )


def upload_conditions(job_spec: JobSpec):
    """Upload the harvested conditions DB, as its own step with its own retry
    policy -- decoupled from run_harvesting_job so a transient condDB failure
    doesn't force re-running ALCAHARVEST."""
    upload_script = job_spec.job_dir / "UPLOAD.sh"
    upload_script.write_text(
        f"""#!/bin/bash -ex

uploadConditions.py {job_spec.final_db_name}
""",
        encoding="utf-8",
    )
    return shell.run_job_script(
        "UPLOAD.sh", job_spec.job_dir, stdout_log="upload_stdout.log", stderr_log="upload_stderr.log"
    )


def finalize_cycle(ctx: RunContext, job_spec: Optional[JobSpec], decision: CycleDecision):
    """Overwrite (not append -- see module docstring) the processed-log with
    the full set harvested this cycle."""
    if job_spec is not None:
        log_path = Path(ctx.working_dir) / PROCESSED_LOG_NAME
        log_path.write_text(
            "\n".join(str(p) for p in sorted(job_spec.input_files, key=str)) + "\n",
            encoding="utf-8",
        )

    return decision.action == "final"
