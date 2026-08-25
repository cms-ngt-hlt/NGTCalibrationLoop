"""Step 2 (RAW -> RECO) processing logic, ported from NGTLoopStep2.py's FSM.

Every function here is stateless: instead of an FSM instance accumulating
`self.setOfLSProcessed` etc. across an in-process while-loop, "already
processed" state is durable on disk (allLSProcessed.log, appended after each
successful launch) and re-read at the top of every cycle. This is what makes
it safe for each cycle to be a fresh Airflow DAG run -- see the plan's "Key
semantic changes" section.

Two hardcoded thresholds are carried over unchanged from the original FSM
(they were never wired to calibration YAML config there either):
`MINIMUM_LS_PER_BATCH` (a batch is "ready" once >=1 new LS file is seen) and
`MAXIMUM_LS_PER_JOB` (only 1 LS file is included per express job, even if
more are waiting -- see NGTLoopStep2.py's PrepareExpressJobs comment "new
logic to avoid gigantic cmsRun jobs").
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, eos, oms, shell

MINIMUM_LS_PER_BATCH = 1
MAXIMUM_LS_PER_JOB = 1

PROCESSED_LOG_NAME = "allLSProcessed.log"
EXPECTED_OUTPUTS_LOG_NAME = "expectedOutputs.log"
RUN_END_LOG_NAME = "runEnd.log"
RUN_START_LOG_NAME = "runStart.log"


@dataclass
class RunContext:
    calibration_name: str
    run_number: int
    run_start_time: datetime
    path_where_files_appear: str
    working_dir: Path
    calib_config: dict
    ngt_params: dict
    max_latch_time_hours: float = 8.0
    min_ls_to_process: int = 1


@dataclass
class CycleDecision:
    action: str  # "batch" | "final" | "wait"
    ls_to_process: set
    still_have_time: bool


@dataclass
class JobSpec:
    working_dir: Path
    script_name: str
    output_file: str
    witness_file: str
    ls_batch: set


def find_new_run(calibration_name):
    """Look for a new run to latch onto via OMS, and if found, create its
    working directory + runStart.log. Returns the run_number, or None."""
    ngt_params = config.load_ngt_parameters()
    calib_config = config.load_calibration_config(calibration_name)
    data_base_path = ngt_params.get("DATA_BASE_PATH", "/data/ngt")
    min_ls_to_process = calib_config["step_2_config"]["minLsToProcess"]

    latched = oms.find_new_run(
        calib_config, data_base_path, calibration_name,
        max_latch_time_hours=8, min_ls_to_process=min_ls_to_process,
    )
    if latched is None:
        return None

    working_dir = Path(data_base_path) / calibration_name / f"run{latched.run_number}"
    working_dir.mkdir(parents=True, exist_ok=True)
    (working_dir / RUN_START_LOG_NAME).write_text(latched.run_start_time.isoformat(), encoding="utf-8")

    logging.info(f"Started processing run {latched.run_number}!")
    return latched.run_number


def build_run_context(calibration_name, run_number):
    """Reconstruct everything a processing cycle needs from disk/config -- the
    equivalent of the FSM's accumulated instance state, rebuilt fresh each
    cycle since there's no long-lived process to carry it in memory."""
    ngt_params = config.load_ngt_parameters()
    calib_config = config.load_calibration_config(calibration_name)
    data_base_path = ngt_params.get("DATA_BASE_PATH", "/data/ngt")
    working_dir = Path(data_base_path) / calibration_name / f"run{run_number}"

    run_start_line = (working_dir / RUN_START_LOG_NAME).read_text(encoding="utf-8")
    run_start_time = datetime.fromisoformat(run_start_line)

    run_str = str(run_number)
    current_run_str = f"{run_str[:3]}/{run_str[3:]}" if len(run_str) == 6 else run_str
    file_in_path = calib_config.get("file_in_path")
    path_where_files_appear = file_in_path + current_run_str + "/00000"

    return RunContext(
        calibration_name=calibration_name,
        run_number=run_number,
        run_start_time=run_start_time,
        path_where_files_appear=path_where_files_appear,
        working_dir=working_dir,
        calib_config=calib_config,
        ngt_params=ngt_params,
        min_ls_to_process=calib_config["step_2_config"]["minLsToProcess"],
    )


def _still_have_time(run_start_time, max_latch_time_hours):
    delta = datetime.now(timezone.utc) - run_start_time
    return delta.total_seconds() < (max_latch_time_hours * 60 * 60)


def _run_has_ended_and_files_are_ready(run_number, run_start_time, max_latch_time_hours, path_where_files_appear):
    is_running, _last_ls = oms.daq_is_running(run_number)
    if is_running:
        return False
    if not _still_have_time(run_start_time, max_latch_time_hours):
        return True  # time's up -> treat as caught up regardless of file counts
    last_ls_oms = oms.last_ls_run_number(run_number)
    last_ls_available = eos.ls_available(path_where_files_appear)
    return abs(int(last_ls_oms) - int(last_ls_available)) <= int(last_ls_oms * 0.04)


def load_already_processed(working_dir):
    log_path = Path(working_dir) / PROCESSED_LOG_NAME
    if not log_path.exists():
        return set()
    return {line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()}


def check_ls_for_processing(ctx: RunContext):
    """Decide what this cycle should do: launch a batch, launch the final
    batch, or keep waiting. Mirrors CheckLSForProcessing + the
    ContinueAfterCheckLS transition conditions (WeStillHaveTime,
    ThereAreLSWaiting/ThereAreEnoughLS, RunHasEndedAndFilesAreReady), in the
    same precedence order as the original transition list."""
    already_processed = load_already_processed(ctx.working_dir)
    available = set(eos.list_available_files(ctx.path_where_files_appear))
    ls_to_process = available - already_processed

    still_have_time = _still_have_time(ctx.run_start_time, ctx.max_latch_time_hours)
    enough_ls = len(ls_to_process) >= MINIMUM_LS_PER_BATCH

    if not still_have_time:
        return CycleDecision(action="final", ls_to_process=ls_to_process, still_have_time=False)

    if ls_to_process and enough_ls:
        return CycleDecision(action="batch", ls_to_process=ls_to_process, still_have_time=True)

    if _run_has_ended_and_files_are_ready(
        ctx.run_number, ctx.run_start_time, ctx.max_latch_time_hours, ctx.path_where_files_appear
    ):
        return CycleDecision(action="final", ls_to_process=ls_to_process, still_have_time=True)

    return CycleDecision(action="wait", ls_to_process=ls_to_process, still_have_time=True)


def prepare_express_job(ctx: RunContext, ls_to_process) -> Optional[JobSpec]:
    """Write the cmsDriver bash script for the Express step-2 job covering up
    to MAXIMUM_LS_PER_JOB of the given lumisections. Returns None if there's
    nothing to process (mirrors PrepareExpressJobs' early return)."""
    if not ls_to_process:
        return None

    if len(ls_to_process) > MAXIMUM_LS_PER_JOB:
        express_ls = set(sorted(ls_to_process)[:MAXIMUM_LS_PER_JOB])
    else:
        express_ls = set(ls_to_process)

    ls_numbers = set()
    for file_path in express_ls:
        result = eos.edm_file_util(str(file_path))
        for match in re.finditer(r"^\s*\d+\s+(\d+)\s+", result.stdout, re.MULTILINE):
            ls_numbers.add(int(match.group(1)))

    min_ls = min(ls_numbers, default=None)
    max_ls = max(ls_numbers, default=None)
    step_args = ctx.calib_config["step_2_config"]
    output_affix = step_args["output_filename_affix"]

    # Deterministic (content-hashed), not random: this function gets called more
    # than once per cycle across different Airflow tasks that each rebuild the
    # JobSpec idempotently (see airflow_dags/ngt_dags.py's _prepare helper) --
    # a random affix would litter a fresh, orphaned script per call instead of
    # reproducing the same one, exactly the problem the job-dir content-hashing
    # in step3.py/step4.py was already introduced to avoid.
    temp_affix = hashlib.sha1(
        "|".join(str(p) for p in sorted(express_ls, key=str)).encode("utf-8")
    ).hexdigest()[:10]
    temp_script_name = "cmsDriver_" + temp_affix + ".sh"
    affix = f"LS{min_ls:04d}To{max_ls:04d}"
    log_filename = f"run{ctx.run_number}_{affix}_step2.log"
    temp_output_filename = "output_" + temp_affix + ".root"
    output_filename = f"run{ctx.run_number}_{affix}{output_affix}.root"
    python_filename = f"run{ctx.run_number}_{affix}{output_affix}.py"
    witness_file = f"run{ctx.run_number}_{affix}{output_affix}_job.txt"
    filein_paths = ",".join("root://eoscms.cern.ch/" + str(p) for p in express_ls)

    ngt_params = ctx.ngt_params
    scram_arch = ngt_params["SCRAM_ARCH"]
    cmssw_version = ngt_params["CMSSW_VERSION"]
    global_tag = ngt_params["GLOBAL_TAG"]

    script_path = Path(ctx.working_dir) / temp_script_name
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(
            f"""#!/bin/bash -ex

export $SCRAM_ARCH={scram_arch}
cmsrel {cmssw_version}
cd {cmssw_version}/src
cmsenv
cd -

cmsDriver.py expressStep2 --conditions {global_tag} \\
-s {step_args["step"]} --datatier {step_args["datatier"]} \\
--eventcontent {step_args["eventcontent"]} --data --process {step_args["process"]} \\
"""
        )
        if "procModifier" in step_args:
            f.write(f"""--procModifier {step_args["procModifier"]} """)
        f.write(
            f"""--scenario {step_args["scenario"]} \\
--era {step_args["era"]} \\
--nThreads 8 --nStreams 8 -n -1 \\
--filein {filein_paths} --fileout file:{temp_output_filename} --no_exec \\
--python_filename {python_filename}

if cmsRun {python_filename} > {log_filename} 2>&1; then
  mv {temp_output_filename} {output_filename}
  touch {witness_file}
else
  echo 'cmsRun failed' >> {log_filename}
  exit 1
fi

rm {temp_script_name}
"""
        )

    logging.info(f"Prepared file {temp_script_name}")
    return JobSpec(
        working_dir=ctx.working_dir,
        script_name=temp_script_name,
        output_file=output_filename,
        witness_file=witness_file,
        ls_batch=express_ls,
    )


def run_express_job(job_spec: JobSpec):
    """Run the prepared express job script to completion (blocking). Raises
    shell.JobScriptFailedError on failure, which is what lets the Airflow task
    calling this retry per its own retry policy."""
    return shell.run_job_script(job_spec.script_name, job_spec.working_dir)


def finalize_cycle(ctx: RunContext, job_spec: Optional[JobSpec], decision: CycleDecision):
    """Durably record a successfully-launched batch, and write runEnd.log if
    this was the final cycle. Called only after run_express_job succeeds (or
    was skipped because there was nothing to process)."""
    if job_spec is not None:
        log_path = Path(ctx.working_dir) / PROCESSED_LOG_NAME
        with open(log_path, "a", encoding="utf-8") as f:
            for ls in sorted(job_spec.ls_batch):
                f.write(str(ls) + "\n")
        outputs_path = Path(ctx.working_dir) / EXPECTED_OUTPUTS_LOG_NAME
        with open(outputs_path, "a", encoding="utf-8") as f:
            f.write("file:" + str(Path(ctx.working_dir) / job_spec.output_file) + "\n")

    if decision.action == "final":
        logging.info(f"Processing of run {ctx.run_number} has ended. Creating runEnd.log...")
        (Path(ctx.working_dir) / RUN_END_LOG_NAME).touch()

    return decision.action == "final"
