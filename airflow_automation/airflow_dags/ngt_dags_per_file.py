"""Airflow DAG design for the NGT calibration loop: one run-level detector,
per-calibration deferred file detection, and one processing DAG run per input
file. Built from the exact same ngt_calibration_loop libraries and the exact
same fake OMS/EOS live-test setup (scenario-player/seed.py,
scenario-player/scenario_player.py, scenario-player/scenarios/*.yaml)
completely unmodified. See ngt_dags_watch.py for an event-driven
(AssetWatcher) alternative that schedules the same processing logic, and
README.md's "Airflow DAG designs" section for how to run/compare them.

Five DAGs for 3 calibrations (`ngt_perfile_*`): one run_detector, one
file_detector, and one process DAG *per calibration*.

- ngt_perfile_run_detector -- cron-scheduled (every 30s), one task per
  calibration, each calling step2.find_new_run(calibration) unchanged (same
  OMS query + working-dir-existence dedup). When a calibration latches a new
  run, this dispatches one ngt_perfile_file_detector run for (that
  calibration, that run).

- ngt_perfile_file_detector -- schedule=None, conf={"calibration",
  "run_number", "cycle"}. One deferred task (wait_for_files, see triggers.py)
  waits -- without occupying a worker slot -- for the next new raw input file
  for this calibration/run to appear (or the run to end with nothing left),
  reusing step2.check_ls_for_processing unchanged as the primary "is there
  something yet" check. That check's own give-up timer is measured from run
  *start*, so wait_for_files' trigger additionally queries OMS directly and
  stops waiting once the run has been *ended* for RUN_END_GRACE_SECONDS,
  regardless of how much of check_ls_for_processing's own budget is left --
  see triggers.NewFileTrigger's docstring for why that decision lives there
  rather than as a change to step2.py's shared logic. dispatch_and_continue
  then triggers one ngt_perfile_process_{calibration} run per newly-found
  file (deterministic run_id, see airflow_api.trigger_dag_run's docstring on
  why that's a safe dedup mechanism here) and either retriggers itself for
  the next cycle, or, if the run just ended, touches runEnd.log (via
  step2.finalize_cycle, job_spec=None) and stops.

- ngt_perfile_process_{calibration} (one DAG per calibration -- e.g.
  ngt_perfile_process_ecalpedestals -- built from one shared factory,
  _build_process_dag(calibration), so they're separately visible/
  pausable/graphable in the Airflow UI rather than one DAG whose runs for
  different calibrations are only distinguishable by conf) -- schedule=None,
  conf={"run_number", "file"}. Runs one input file through all three steps:
  step2_express (RAW -> RECO for just this file) -> step3_alca (RECO ->
  ALCARECO for just this file) -> step4_harvest -> step4_upload. Step 4 is
  the one deliberate exception to "per file": per this design's explicit
  scoping decision, Step 2 and Step 3 each act on only the single file this
  DAG run was triggered for, but Step 4 harvests/uploads from *every*
  ALCARECO file accumulated for the run so far (more statistics genuinely
  improve the payload -- this is exactly step4.py's own pre-existing
  "reprocess everything" behavior, see its module docstring, reused
  completely unchanged here). The execution flow is still one DAG run per
  file -- Step 4 just happens to look at more than its own triggering file's
  data each time.

Why "defer" specifically (not just a shorter poll_interval on a cron-scheduled
DAG): a real run can take hours, and
this design has one file-detector DagRun in flight per (calibration, run)
for that whole time. A deferred task's wait lives in Airflow's `triggerer`
process (async, many trigger instances multiplexed on one event loop) rather
than occupying a worker slot for the duration -- see triggers.py's
NewFileTrigger. This means running this design needs a long-lived
`airflow triggerer` process (airflow_automation/airflow_demo/airflow_demo.sh's
cmd_start launches it unconditionally, alongside the scheduler/dag-processor/
API server, since it's harmless idle overhead when unused).

Fewer, more dynamic DAGs vs. more, simpler DAGs: this design has 5 DAG
*definitions* total for 3 calibrations, because
ngt_perfile_file_detector stays one generic definition parameterized by
`calibration` via trigger conf, rather than one definition per calibration
the way ngt_perfile_process_{calibration} is -- there's no
UI/observability reason to split file_detector per calibration the way there
is for process (it has no per-calibration task content to look at; its
DagRuns are already distinguishable by conf/run_id, and the whole point of
having only one is that its wait is what's expensive to duplicate needlessly
-- see "Why defer" below), whereas process's actual job output/logs are what
an operator wants to browse per calibration in the UI. Each calibration's own
input-file path still comes from that calibration's own calibrationYAML
(calib_config["file_in_path"], loaded inside step{2,3,4}.build_run_context
exactly as before), so "each calibration has a separate set of input files"
is respected either way -- config lookup either way, just also a separate
DAG definition for process specifically.
"""

import logging
from datetime import datetime, timedelta, timezone

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, BaseOperator
from airflow.sdk.exceptions import AirflowSkipException

# Per-step processing logic + the deterministic per-file run_id scheme + the
# shared retry policy / constants live in _perfile_process.py so ngt_dags_watch.py
# can reuse them without importing this module (which would make Airflow's
# DagBag double-bag the DAGs below). Re-exported here under the names this
# module (and tests/airflow_designs/test_dags_per_file.py) already use.
from airflow_dags import _perfile_process as _pp
from airflow_dags import airflow_api
from airflow_dags._perfile_process import (
    CALIBRATIONS,
    DAG_START_DATE,
    DEFAULT_ARGS,
    FILE_POLL_INTERVAL_SECONDS,
    RUN_END_GRACE_SECONDS,
)
from airflow_dags.triggers import NewFileTrigger
from ngt_calibration_loop import step2

RUN_DETECTOR_DAG_ID = "ngt_perfile_run_detector"
FILE_DETECTOR_DAG_ID = "ngt_perfile_file_detector"


def _process_dag_id(calibration):
    return f"ngt_perfile_process_{calibration.lower()}"


RUN_DETECTOR_SCHEDULE = timedelta(seconds=30)

_LAUNCH_RETRY_KWARGS = _pp.LAUNCH_RETRY_KWARGS
_UPLOAD_RETRY_KWARGS = _pp.UPLOAD_RETRY_KWARGS
_extract_ls_number = _pp.extract_ls_number
_job_run_id_for_file = _pp.job_run_id_for_file
_make_step2_express_callable = _pp.make_step2_express_callable
_make_step3_alca_callable = _pp.make_step3_alca_callable
_make_step4_harvest_callable = _pp.make_step4_harvest_callable
_make_step4_upload_callable = _pp.make_step4_upload_callable


# --- run detector DAG task --------------------------------------------------------------


def _make_run_detector_callable(calibration):
    def _detect(**_context):
        run_number = step2.find_new_run(calibration)
        if run_number is None:
            raise AirflowSkipException(f"No new run for {calibration} this tick")

        run_id = f"{calibration.lower()}_run{run_number}__cycle0"
        result = airflow_api.trigger_dag_run(
            FILE_DETECTOR_DAG_ID, run_id, conf={"calibration": calibration, "run_number": str(run_number), "cycle": 0}
        )
        if result is None:
            logging.info(f"{run_id} already existed -- file detection already running for {calibration} run {run_number}")
        else:
            logging.info(f"Dispatched file detection for {calibration} run {run_number}")

    return _detect


# --- file detector DAG tasks -------------------------------------------------------------


class WaitForNewFilesOperator(BaseOperator):
    """Defers immediately (see triggers.NewFileTrigger) to watch for the next
    new raw input file for this DagRun's calibration/run_number, without
    occupying a worker slot while it waits -- see this module's docstring for
    why that matters here specifically."""

    def __init__(
        self,
        *,
        poll_interval: float = FILE_POLL_INTERVAL_SECONDS,
        run_end_grace_seconds: float = RUN_END_GRACE_SECONDS,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.poll_interval = poll_interval
        self.run_end_grace_seconds = run_end_grace_seconds

    def execute(self, context):
        conf = context["dag_run"].conf
        self.defer(
            trigger=NewFileTrigger(
                conf["calibration"], int(conf["run_number"]), self.poll_interval, self.run_end_grace_seconds
            ),
            method_name="execute_complete",
            # generous outer bound, well above both step2's own 8h max_latch_time_hours and this
            # trigger's own (much shorter) run-end grace period -- a backstop for the case neither
            # give-up mechanism ever fires
            timeout=timedelta(hours=10),
        )

    def execute_complete(self, context, event=None):  # pylint: disable=unused-argument  # signature fixed by Airflow
        return event  # {"action": "batch"|"final", "files": [str, ...]}


def _dispatch_processing_dag(calibration, run_number, file_path):
    run_id = _job_run_id_for_file(run_number, file_path)
    result = airflow_api.trigger_dag_run(
        _process_dag_id(calibration),
        run_id,
        conf={"run_number": str(run_number), "file": str(file_path)},
    )
    if result is None:
        logging.info(f"{run_id} already existed -- {file_path} already has a processing run")
    else:
        logging.info(f"Dispatched processing for {calibration} run {run_number} file={file_path}")


def _make_dispatch_and_continue_callable():
    def _dispatch_and_continue(**context):
        conf = context["dag_run"].conf
        calibration = conf["calibration"]
        run_number = conf["run_number"]
        cycle = conf.get("cycle", 0)
        event = context["ti"].xcom_pull(task_ids="wait_for_files")
        action = event["action"]
        files = event["files"]

        for file_path in sorted(files):
            _dispatch_processing_dag(calibration, run_number, file_path)

        if action == "final":
            # job_spec=None -> finalize_cycle only touches runEnd.log, it
            # doesn't append anything to allLSProcessed.log (that already
            # happened per-file, inside ngt_perfile_process's step2_express
            # task) -- see step2.finalize_cycle.
            ctx = step2.build_run_context(calibration, int(run_number))
            step2.finalize_cycle(
                ctx, None, step2.CycleDecision(action="final", ls_to_process=set(), still_have_time=False)
            )
            logging.info(
                f"File detection for {calibration} run {run_number} concluded "
                f"({len(files)} file(s) dispatched this final cycle)"
            )
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        next_run_id = f"{calibration.lower()}_run{run_number}__cycle{cycle + 1}__{stamp}"
        airflow_api.trigger_dag_run(
            FILE_DETECTOR_DAG_ID,
            next_run_id,
            conf={"calibration": calibration, "run_number": run_number, "cycle": cycle + 1},
        )

    return _dispatch_and_continue


# --- processing DAG tasks (run per input file) --------------------------------------------
#
# step2_express / step3_alca / step4_harvest / step4_upload callables now live
# in airflow_automation/airflow_dags/_perfile_process.py (shared verbatim with ngt_dags_watch.py);
# re-exported near the top of this module under the _make_step*_callable names.


# --- DAG assembly ------------------------------------------------------------------------


def _build_run_detector_dag():
    with DAG(
        dag_id=RUN_DETECTOR_DAG_ID,
        description="Check OMS for new runs (all calibrations) and dispatch per-calibration file detection",
        schedule=RUN_DETECTOR_SCHEDULE,
        start_date=DAG_START_DATE,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULT_ARGS,
        tags=["ngt", "per-file", "run-detector"],
    ) as dag:
        for calibration in CALIBRATIONS:
            PythonOperator(
                task_id=f"detect_{calibration.lower()}",
                python_callable=_make_run_detector_callable(calibration),
            )
    return dag


def _build_file_detector_dag():
    with DAG(
        dag_id=FILE_DETECTOR_DAG_ID,
        description="Deferred-wait for the next new input file for one calibration/run, dispatch a processing run, repeat",
        schedule=None,
        start_date=DAG_START_DATE,
        catchup=False,
        max_active_runs=10,  # one (calibration, run) chain per active DagRun
        default_args=DEFAULT_ARGS,
        tags=["ngt", "per-file", "file-detector"],
    ) as dag:
        wait_for_files = WaitForNewFilesOperator(task_id="wait_for_files", retries=2, retry_delay=timedelta(minutes=1))
        dispatch_and_continue = PythonOperator(
            task_id="dispatch_and_continue", python_callable=_make_dispatch_and_continue_callable()
        )
        wait_for_files >> dispatch_and_continue
    return dag


def _build_process_dag(calibration):
    with DAG(
        dag_id=_process_dag_id(calibration),
        description=f"Run one {calibration} input file through Step 2, 3 and 4 "
        "(Step 4 over every file accumulated for the run so far)",
        schedule=None,
        start_date=DAG_START_DATE,
        catchup=False,
        max_active_runs=10,  # many files/runs may be processing concurrently
        default_args=DEFAULT_ARGS,
        tags=["ngt", "per-file", "process", calibration],
    ) as dag:
        step2_express = PythonOperator(
            task_id="step2_express", python_callable=_make_step2_express_callable(calibration), **_LAUNCH_RETRY_KWARGS
        )
        step3_alca = PythonOperator(
            task_id="step3_alca", python_callable=_make_step3_alca_callable(calibration), **_LAUNCH_RETRY_KWARGS
        )
        step4_harvest = PythonOperator(
            task_id="step4_harvest", python_callable=_make_step4_harvest_callable(calibration), **_LAUNCH_RETRY_KWARGS
        )
        step4_upload = PythonOperator(
            task_id="step4_upload", python_callable=_make_step4_upload_callable(calibration), **_UPLOAD_RETRY_KWARGS
        )
        step2_express >> step3_alca >> step4_harvest >> step4_upload
    return dag


globals()[RUN_DETECTOR_DAG_ID] = _build_run_detector_dag()
globals()[FILE_DETECTOR_DAG_ID] = _build_file_detector_dag()
for _calibration in CALIBRATIONS:
    globals()[_process_dag_id(_calibration)] = _build_process_dag(_calibration)
