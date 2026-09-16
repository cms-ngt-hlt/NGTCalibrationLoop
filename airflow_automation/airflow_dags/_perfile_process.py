"""Shared, DAG-free processing helpers for the Airflow DAG designs that run one
input file through Step 2 -> Step 3 -> Step 4 (`ngt_dags_per_file.py` and
`ngt_dags_watch.py`).

Both designs use the *exact same* per-task logic and retry policy -- only
how/when a processing DagRun gets triggered differs between them
(trigger_dag_run from a deferred file-detector vs. an Asset-event-woken one).
This module holds everything those two designs would otherwise duplicate: the
per-step `python_callable` factories, the deterministic per-file `run_id`
scheme, and the retry-kwargs / constants they share.

It deliberately defines **no DAGs** -- so `ngt_dags_watch.py` can
`from airflow_dags._perfile_process import ...` without Airflow's DagBag
double-bagging `ngt_dags_per_file.py`'s DAGs (which is exactly what a plain
`import airflow_dags.ngt_dags_per_file` from another DAG file caused). See
`ngt_dags_per_file.py`'s module docstring for the design-level explanation of
what each step callable does and why Step 4 is the "reprocess everything"
exception.
"""

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from airflow.sdk.exceptions import AirflowSkipException

from airflow_dags.triggers import DEFAULT_RUN_END_GRACE_SECONDS
from ngt_calibration_loop import step2, step3, step4

CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")

FILE_POLL_INTERVAL_SECONDS = float(os.environ.get("NGT_FILE_POLL_SECONDS", 5))
# How long wait_for_files keeps waiting after OMS reports a run has ended
# before giving up -- see triggers.NewFileTrigger's docstring for why this is
# a separate, run-end-relative timer from step2's own (run-start-relative)
# max_latch_time_hours. airflow_automation/airflow_demo/airflow_demo.sh's demo_env_exports overrides this
# to something short for the live-test setup.
RUN_END_GRACE_SECONDS = float(
    os.environ.get("NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS", DEFAULT_RUN_END_GRACE_SECONDS)
)
DAG_START_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
DEFAULT_ARGS = {"owner": "ngt-calibration"}

# Applied to every task that actually launches a CMSSW job (step2/3's own job,
# step4's harvest) -- same policy regardless of step or design.
LAUNCH_RETRY_KWARGS = dict(
    retries=3, retry_delay=timedelta(minutes=2), retry_exponential_backoff=True, execution_timeout=timedelta(hours=2)
)
# Decoupled from the above so a transient condDB upload failure retries on its
# own without redoing ALCAHARVEST.
UPLOAD_RETRY_KWARGS = dict(
    retries=5, retry_delay=timedelta(minutes=1), retry_exponential_backoff=True, execution_timeout=timedelta(minutes=30)
)


# scenario-player/seed.py's _touch_ls_file names raw input files run{run}_ls{ls:04d}.root
# -- matched best-effort so the lumisection number can appear in the
# processing DagRun's run_id (see job_run_id_for_file), purely for readability
# in the Airflow UI/CLI. A real, differently-named EOS file simply won't
# match, which is fine -- the content hash below still guarantees uniqueness
# on its own either way.
_LS_NUMBER_RE = re.compile(r"_ls0*(\d+)", re.IGNORECASE)


def extract_ls_number(file_path):
    match = _LS_NUMBER_RE.search(Path(file_path).name)
    return int(match.group(1)) if match else None


def run_and_file_from_context(context):
    """Return ``(run_number, file_path)`` for a per-file process task, from
    whichever of the two sources this design uses:

    * ``dag_run.conf`` -- ngt_dags_per_file.py, which
      ``trigger_dag_run`` the process DAG with ``conf={"run_number", "file"}``;
    * the triggering Asset event's ``extra`` -- ngt_dags_watch.py, whose
      ``ngt_watch_process_<cal>`` DAGs are ``schedule=[Asset("ngt://files/<cal>")]``
      and get ``{"run_number", "file"}`` from ``LumisectionFileWatcherTrigger``.

    Raises ``AirflowSkipException`` if neither is present (e.g. a bare manual
    trigger) -- there is nothing for a per-file task to act on."""
    dag_run = context.get("dag_run")
    conf = getattr(dag_run, "conf", None) or {}
    if "file" in conf and "run_number" in conf:
        return str(conf["run_number"]), str(conf["file"])

    events_by_asset = context.get("triggering_asset_events") or {}
    all_events = [event for events in events_by_asset.values() for event in events]
    if not all_events:
        raise AirflowSkipException("no dag_run.conf and no triggering Asset event -- nothing to process")

    def _ts(event):
        return getattr(event, "timestamp", None) or datetime.min.replace(tzinfo=timezone.utc)

    extra = dict(getattr(max(all_events, key=_ts), "extra", None) or {})
    if "file" not in extra and isinstance(extra.get("payload"), dict):  # watcher-wrapped shape
        extra = extra["payload"]
    return str(extra["run_number"]), str(extra["file"])


def job_run_id_for_file(run_number, file_path):
    # Content-hashed, not sequential -- same reasoning as step3.py/step4.py's
    # job-dir naming: a deterministic run_id lets the caller be invoked more
    # than once for the same file (a re-discovered file across file-detector
    # cycles, a retried dispatch task) and have Airflow's own duplicate-run_id
    # rejection silently no-op the repeats (see airflow_api.trigger_dag_run's
    # docstring) instead of double-processing it. No calibration prefix --
    # each process DagRun already lives under a calibration-specific dag_id.
    # The lumisection number is folded in too (when parseable) purely for
    # readability -- e.g. "run398800__ls0051__<hash>"; dedup safety comes
    # entirely from the hash, not this part.
    digest = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()[:12]
    ls_number = extract_ls_number(file_path)
    ls_part = f"ls{ls_number:04d}__" if ls_number is not None else ""
    return f"run{run_number}__{ls_part}{digest}"


# --- processing DAG task callables (run per input file) ------------------------------


def make_step2_express_callable(calibration):
    def _step2_express(**context):
        # No existence check before prepare_express_job: unlike step3/4's
        # prepare_*_job (which filter their input set down to whatever still
        # exists locally and return None if that's empty), step2's own EOS
        # input files aren't local paths at all (they're resolved via
        # edmFileUtil/xrdfs -- see ngt_calibration_loop.eos) and are never
        # deleted by anything else in this pipeline, so
        # step2.prepare_express_job doesn't defend against this either.
        run_number, file_path = run_and_file_from_context(context)

        ctx2 = step2.build_run_context(calibration, int(run_number))
        # Belt-and-braces for the watcher design (ngt_dags_watch.py): its file
        # watcher can re-yield a file in the window before allLSProcessed.log is
        # written, and there's no trigger_dag_run run_id to 409-dedupe on -- so
        # skip a file that's already recorded as processed. A no-op for the
        # trigger_dag_run designs (deterministic run_ids already prevent it).
        if str(Path(file_path)) in step2.load_already_processed(ctx2.working_dir):
            raise AirflowSkipException(f"{file_path} already processed for {calibration} run {run_number}")
        job_spec = step2.prepare_express_job(ctx2, {Path(file_path)})
        step2.run_express_job(job_spec)
        step2.finalize_cycle(
            ctx2, job_spec, step2.CycleDecision(action="batch", ls_to_process={Path(file_path)}, still_have_time=True)
        )

        output_path = str(Path(ctx2.working_dir) / job_spec.output_file)
        context["ti"].xcom_push(key="step2_output", value=output_path)
        logging.info(f"[{calibration} run {run_number}] Step 2 produced {output_path}")

    return _step2_express


def make_step3_alca_callable(calibration):
    def _step3_alca(**context):
        run_number, _file = run_and_file_from_context(context)
        step2_output = context["ti"].xcom_pull(task_ids="step2_express", key="step2_output")

        ctx3 = step3.build_run_context(calibration, str(run_number))
        job_spec = step3.prepare_alca_prompt_job(ctx3, {Path(step2_output)})
        if job_spec is None:
            raise AirflowSkipException(f"Step 2 output no longer available: {step2_output}")

        step3.run_alca_prompt_job(job_spec)
        step3.finalize_cycle(
            ctx3, job_spec, step3.CycleDecision(action="batch", files_to_process={Path(step2_output)})
        )
        logging.info(f"[{calibration} run {run_number}] Step 3 produced ALCARECO from {step2_output}")

    return _step3_alca


def make_step4_harvest_callable(calibration):
    def _step4_harvest(**context):
        run_number, _file = run_and_file_from_context(context)

        ctx4 = step4.build_run_context(calibration, str(run_number))
        # Reused completely unchanged: files_to_process is already "every
        # ALCARECO file accumulated for this run so far", every cycle --
        # exactly what this design's Step 4 wants -- see step4.py's module
        # docstring for why that's Step 4's existing behavior.
        decision = step4.check_files_for_processing(ctx4)
        if decision.action == "wait":
            raise AirflowSkipException("Nothing new for Step 4 to harvest yet")

        job_spec = step4.prepare_harvesting_job(ctx4, decision.files_to_process)
        if job_spec is None:
            raise AirflowSkipException("Nothing to harvest (input files vanished)")

        step4.run_harvesting_job(job_spec)
        logging.info(
            f"[{calibration} run {run_number}] Step 4 harvested {len(decision.files_to_process)} accumulated file(s)"
        )

    return _step4_harvest


def make_step4_upload_callable(calibration):
    def _step4_upload(**context):
        # Re-derives the same job (idempotent/deterministic naming, like
        # _step4_harvest) instead of passing the JobSpec through XCom -- cheap
        # and idempotent thanks to the content-hashed job-dir naming.
        run_number, _file = run_and_file_from_context(context)

        ctx4 = step4.build_run_context(calibration, str(run_number))
        decision = step4.check_files_for_processing(ctx4)
        if decision.action == "wait":
            raise AirflowSkipException("Nothing new for Step 4 to upload")

        job_spec = step4.prepare_harvesting_job(ctx4, decision.files_to_process)
        if job_spec is None:
            raise AirflowSkipException("Nothing to upload (input files vanished)")

        step4.upload_conditions(job_spec)
        step4.finalize_cycle(ctx4, job_spec, decision)
        logging.info(f"[{calibration} run {run_number}] Step 4 uploaded conditions")

    return _step4_upload
