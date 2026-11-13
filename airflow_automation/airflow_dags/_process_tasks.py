"""Shared, DAG-free processing helpers for the Airflow DAG design that runs one
input file through Step 2 -> Step 3 -> Step 4 (`ngt_dags_watch.py`).

This module holds the per-step `python_callable` factories and the
retry-kwargs/constants the DAGs use, kept out of any DAG file so the per-task
logic stays independently unit-testable (see
`tests/airflow-ngt/test_dags_watch.py`) and reusable by a future DAG design
without duplication. What each step does is the library's
(`ngt_calibration_loop/run_step.py`, which also runs it outside Airflow): the
callables here only find the DAG run's file and turn "nothing to do" into a skip.

It deliberately defines **no DAGs** -- so a DAG file can
`from airflow_dags._process_tasks import ...` without Airflow's DagBag
double-bagging that file's own DAGs (which is exactly what a plain
`import airflow_dags.<other_dag_module>` from a DAG file caused). See
`ngt_dags_watch.py`'s module docstring for the design-level explanation of what
each step callable does and why Step 4 is the "reprocess everything" exception.
"""

import os
from datetime import datetime, timedelta, timezone

from airflow.sdk.exceptions import AirflowSkipException

from airflow_dags.triggers import DEFAULT_RUN_END_GRACE_SECONDS
from ngt_calibration_loop import run_step

CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")

FILE_POLL_INTERVAL_SECONDS = float(os.environ.get("NGT_FILE_POLL_SECONDS", 5))
# How long a file watcher keeps waiting after OMS reports a run has ended before
# giving up -- see triggers.LumisectionFileWatcherTrigger's docstring for why
# this is a separate, run-end-relative timer from step2's own
# (run-start-relative) max_latch_time_hours.
# airflow_automation/airflow_demo/airflow_demo.sh's demo_env_exports overrides
# it to something short for the live-test setup.
RUN_END_GRACE_SECONDS = float(
    os.environ.get("NGT_RUN_END_GRACE_SECONDS", DEFAULT_RUN_END_GRACE_SECONDS)
)
DAG_START_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
DEFAULT_ARGS = {"owner": "ngt-calibration"}

# Applied to every task that actually launches a CMSSW job (step2/3's own job,
# step4's harvest) -- same policy regardless of step.
LAUNCH_RETRY_KWARGS = dict(
    retries=3, retry_delay=timedelta(minutes=2), retry_exponential_backoff=True, execution_timeout=timedelta(hours=2)
)
# Decoupled from the above so a transient condDB upload failure retries on its
# own without redoing ALCAHARVEST.
UPLOAD_RETRY_KWARGS = dict(
    retries=5, retry_delay=timedelta(minutes=1), retry_exponential_backoff=True, execution_timeout=timedelta(minutes=30)
)
# ngt_dags_watch.py's prime_run queries OMS again (step2.find_new_run, per
# calibration), and by then the run watcher has marked the run as seen and won't
# announce it again -- so a prime_run lost to a passing OMS outage loses the run.
# Short first retries: the run has to still look eligible in OMS when one succeeds.
PRIME_RETRY_KWARGS = dict(
    retries=5, retry_delay=timedelta(seconds=30), retry_exponential_backoff=True, execution_timeout=timedelta(minutes=10)
)


def run_and_file_from_context(context):
    """Return ``(run_number, file_path)`` for a per-file process task, from
    whichever of the two sources is present:

    * the triggering Asset event's ``extra`` -- the normal path for
      ``ngt_dags_watch.py``, whose ``ngt_watch_process_<cal>`` DAGs are
      ``schedule=[Asset("ngt://files/<cal>")]`` and get
      ``{"run_number", "file"}`` from ``LumisectionFileWatcherTrigger``;
    * ``dag_run.conf`` -- a manual/API-triggered run carrying
      ``conf={"run_number", "file"}``, which takes precedence so a single file
      can be deliberately re-processed without waiting for a watcher event.

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


# --- processing DAG task callables (run per input file) ------------------------------


def _call_step(step, calibration, context):
    """Run one of the library's steps (ngt_calibration_loop.run_step) for this DAG run's file. Its result, which it
    also leaves on disk for the next step, becomes the task's return value; nothing to do becomes a skip."""
    run_number, file_path = run_and_file_from_context(context)
    try:
        return step(calibration, run_number, file_path)
    except run_step.NothingToDo as exc:
        raise AirflowSkipException(str(exc)) from exc


def make_step2_express_callable(calibration):
    def _step2_express(**context):
        return _call_step(run_step.step2_express, calibration, context)

    return _step2_express


def make_step3_alca_callable(calibration):
    def _step3_alca(**context):
        return _call_step(run_step.step3_alca, calibration, context)

    return _step3_alca


def make_step4_harvest_callable(calibration):
    def _step4_harvest(**context):
        return _call_step(run_step.step4_harvest, calibration, context)

    return _step4_harvest


def make_step4_upload_callable(calibration):
    def _step4_upload(**context):
        return _call_step(run_step.step4_upload, calibration, context)

    return _step4_upload
