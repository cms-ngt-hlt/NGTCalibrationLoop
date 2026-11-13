"""One step of the per-file pipeline as one call: prepare, run the job script, finalize.

The Airflow DAG design (airflow_automation/airflow_dags/ngt_dags_watch.py) runs every input file through Step 2,
Step 3, Step 4's harvest and Step 4's upload, one task each. What each of those does -- rebuild the run's context
from disk, decide whether there's anything to do, write the job script, run it, record what was processed -- lives
here rather than in the DAGs, so the same code runs wherever the step runs: in an Airflow worker (the DAGs'
callables call these functions), in a pod of another image, such as a CMSSW one
(`python3 -m ngt_calibration_loop.run_step <step> --calibration ... --run ... --file ...`), or in a test.

A step hands the next one its result through a small JSON file in the run's working directory
(`.ngt-results/<step>-<key>.json`, keyed by the input file), not through the engine, so a step that ran in a pod
needs no channel back to Airflow. Each step removes its own previous result before it starts, so a later step only
ever reads what this pass produced. A step that finds nothing to do raises NothingToDo, which the command line
reports as exit code 99 and the DAGs turn into a skip.
"""

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

from . import step2, step3, step4

RESULTS_DIR_NAME = ".ngt-results"
EXIT_NOTHING_TO_DO = 99


class NothingToDo(Exception):
    """The step has nothing to do for this file: already processed, its input gone, nothing new to harvest."""


def result_path(working_dir, step, file_path):
    key = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()[:12]
    return Path(working_dir) / RESULTS_DIR_NAME / f"{step}-{key}.json"


def read_result(working_dir, step, file_path):
    """What `step` recorded for this file, or None if it hasn't (or found nothing to do)."""
    path = result_path(working_dir, step, file_path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write_result(working_dir, step, file_path, result):
    path = result_path(working_dir, step, file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    partial.replace(path)
    return result


def _clear_result(working_dir, step, file_path):
    result_path(working_dir, step, file_path).unlink(missing_ok=True)


def step2_express(calibration, run_number, file_path):
    """Step 2's express job for one input file, run to completion and recorded as processed."""
    ctx = step2.build_run_context(calibration, int(run_number))
    _clear_result(ctx.working_dir, "step2_express", file_path)
    # The file watcher can hand out a file again in the window before allLSProcessed.log records it, and an
    # Asset-triggered DAG run has no deterministic run_id for Airflow to reject a duplicate of.
    if str(Path(file_path)) in step2.load_already_processed(ctx.working_dir):
        raise NothingToDo(f"{file_path} already processed for {calibration} run {run_number}")

    job = step2.prepare_express_job(ctx, {Path(file_path)})
    step2.run_express_job(job)
    step2.finalize_cycle(
        ctx, job, step2.CycleDecision(action="batch", ls_to_process={Path(file_path)}, still_have_time=True)
    )
    output = str(Path(ctx.working_dir) / job.output_file)
    logging.info(f"[{calibration} run {run_number}] Step 2 produced {output}")
    return _write_result(ctx.working_dir, "step2_express", file_path, {"step2_output": output})


def step3_alca(calibration, run_number, file_path):
    """Step 3's ALCARECO job on what Step 2 produced from this file."""
    ctx = step3.build_run_context(calibration, str(run_number))
    _clear_result(ctx.working_dir, "step3_alca", file_path)
    previous = read_result(ctx.working_dir, "step2_express", file_path)
    if previous is None:
        raise NothingToDo(f"Step 2 recorded no output for {file_path}")
    step2_output = Path(previous["step2_output"])

    job = step3.prepare_alca_prompt_job(ctx, {step2_output})
    if job is None:
        raise NothingToDo(f"Step 2 output no longer available: {step2_output}")
    step3.run_alca_prompt_job(job)
    step3.finalize_cycle(ctx, job, step3.CycleDecision(action="batch", files_to_process={step2_output}))
    logging.info(f"[{calibration} run {run_number}] Step 3 produced ALCARECO from {step2_output}")
    return _write_result(ctx.working_dir, "step3_alca", file_path, {"step3_job_dir": str(job.job_dir)})


def step4_harvest(calibration, run_number, file_path):
    """Step 4's harvest over every ALCARECO file accumulated for the run so far (step4.py's own behaviour)."""
    ctx = step4.build_run_context(calibration, str(run_number))
    _clear_result(ctx.working_dir, "step4_harvest", file_path)
    decision = step4.check_files_for_processing(ctx)
    if decision.action == "wait":
        raise NothingToDo("Nothing new for Step 4 to harvest yet")

    job = step4.prepare_harvesting_job(ctx, decision.files_to_process)
    if job is None:
        raise NothingToDo("Nothing to harvest (input files vanished)")
    step4.run_harvesting_job(job)
    harvested = sorted(str(p) for p in job.input_files)
    logging.info(f"[{calibration} run {run_number}] Step 4 harvested {len(harvested)} accumulated file(s)")
    return _write_result(ctx.working_dir, "step4_harvest", file_path, {"harvested_files": harvested})


def step4_upload(calibration, run_number, file_path):
    """Upload the payload this file's harvest produced.

    The harvested set comes from step4_harvest's result, never from recomputing it. The job directory is named after
    a hash of that set, and the set grows as Step 3 keeps delivering ALCARECO files, so a recomputed one could name a
    directory whose harvest hasn't run (found live: an upload of a payload covering no lumisections), or another
    harvest's payload.
    """
    ctx = step4.build_run_context(calibration, str(run_number))
    _clear_result(ctx.working_dir, "step4_upload", file_path)
    harvested = (read_result(ctx.working_dir, "step4_harvest", file_path) or {}).get("harvested_files")
    if not harvested:
        raise NothingToDo("Nothing harvested for Step 4 to upload")

    job = step4.prepare_harvesting_job(ctx, {Path(p) for p in harvested})
    if job is None:
        raise NothingToDo("Nothing to upload (harvested files vanished)")
    step4.upload_conditions(job)
    # The decision only gives finalize_cycle its is-final flag; the processed-log it writes comes from
    # job.input_files, i.e. what was harvested and uploaded above.
    step4.finalize_cycle(ctx, job, step4.check_files_for_processing(ctx))
    logging.info(f"[{calibration} run {run_number}] Step 4 uploaded conditions")
    return _write_result(ctx.working_dir, "step4_upload", file_path, {"payload": str(job.job_dir / job.final_db_name)})


STEPS = {
    "step2_express": step2_express,
    "step3_alca": step3_alca,
    "step4_harvest": step4_harvest,
    "step4_upload": step4_upload,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ngt_calibration_loop.run_step",
        description="Run one step of the per-file pipeline. Exit 0: done (its result is printed); "
        f"{EXIT_NOTHING_TO_DO}: nothing to do; anything else: it failed.",
    )
    parser.add_argument("step", choices=list(STEPS))
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--run", required=True, dest="run_number")
    parser.add_argument("--file", required=True, dest="file_path", help="the input file this pass is for")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        result = STEPS[args.step](args.calibration, args.run_number, args.file_path)
    except NothingToDo as exc:
        print(f"nothing to do: {exc}", file=sys.stderr)
        return EXIT_NOTHING_TO_DO
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
