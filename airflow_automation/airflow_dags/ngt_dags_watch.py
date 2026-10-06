"""Airflow DAG design for the NGT calibration loop: a fully
**event-driven, all-static-asset** variant -- no cron/polling DAGs at all, no
dynamically-named assets. Built from the exact same unmodified
ngt_calibration_loop libraries and the same fake OMS/EOS/CMSSW live-test
setup the project's pytest suite and live demo already used. Full writeup,
including an overview of the Airflow 3 Asset machinery this is built on and
why per-run assets are not an option: watch_asset_scheduling_design.md.

Four static Assets, four AssetWatchers, four DAGs, zero cron:

- Asset("ngt://runs")  with  AssetWatcher(RunWatcherTrigger) -- the triggerer
  runs RunWatcherTrigger continuously (while ngt_watch_run_primer is
  unpaused): it polls OMS for a new PROTONS/collisions run (calibration-
  agnostic -- the filltype/l1hltmode filter is identical across all three
  calibration YAMLs) and yields one TriggerEvent per genuinely-new run,
  updating Asset("ngt://runs"). See triggers.RunWatcherTrigger.

- ngt_watch_run_primer  --  schedule=[Asset("ngt://runs")]. One task: calls
  step2.find_new_run(calibration) for each of the three calibrations
  (unchanged, idempotent) -- which creates DATA_BASE_PATH/<cal>/run<N>/ +
  runStart.log. That on-disk state is what the file watchers gate on, so a
  run-asset update is what "starts" per-calibration file watching (the
  watchers themselves are always running -- an AssetWatcher can't be spawned
  by an event -- they're just idle until a run dir appears).

- Asset("ngt://files/<cal>")  with  AssetWatcher(LumisectionFileWatcherTrigger)
  x3 -- each watches its calibration's currently-active run dir (newest
  run*/ with runStart.log and no runEnd.log) for new raw input files, and
  yields one TriggerEvent **per file** (reusing step2.check_ls_for_processing
  unchanged), updating Asset("ngt://files/<cal>"). On the run ending it writes
  runEnd.log (step2.finalize_cycle) and goes idle. Files already handed out
  are remembered in the Asset's AssetStateStore (see the trigger's docstring).

- ngt_watch_process_<cal>  --  schedule=[Asset("ngt://files/<cal>")] x3. Runs
  the one file from the triggering Asset event through
  step2_express >> step3_alca >> step4_harvest >> step4_upload. The step
  callables are the shared airflow_automation/airflow_dags/_process_tasks.py
  ones, which read (run_number, file) from the triggering Asset event -- or,
  for a deliberate manual re-run, from dag_run.conf -- via
  _process_tasks.run_and_file_from_context.

Needs the `airflow triggerer` process running (it hosts the watchers).
AssetWatchers only run while their consuming DAG is unpaused -- pausing
ngt_watch_* stops all four watchers.
"""

import logging
from datetime import datetime, timezone

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Asset, AssetWatcher

from airflow_dags import _process_tasks as pp
from airflow_dags import triggers
from ngt_calibration_loop import step2

CALIBRATIONS = pp.CALIBRATIONS

RUN_PRIMER_DAG_ID = "ngt_watch_run_primer"

# Env-overridable poll/grace knobs (airflow_automation/airflow_demo/airflow_demo.sh
# sets short values for the live-test setup).
_WATCH_POLL_SECONDS = pp.FILE_POLL_INTERVAL_SECONDS
_RUN_END_GRACE_SECONDS = pp.RUN_END_GRACE_SECONDS


def _process_dag_id(calibration):
    return f"ngt_watch_process_{calibration.lower()}"


# --- the four static assets + their watchers -----------------------------------------

RUNS_ASSET = Asset(
    name=triggers.RUNS_ASSET_NAME,
    uri=triggers.RUNS_ASSET_URI,
    watchers=[AssetWatcher(name="ngt_run_watch", trigger=triggers.RunWatcherTrigger(poll_interval=_WATCH_POLL_SECONDS))],
)

FILE_ASSETS = {
    calibration: Asset(
        name=triggers.files_asset_name(calibration),
        uri=triggers.files_asset_uri(calibration),
        watchers=[
            AssetWatcher(
                name=f"ngt_{calibration.lower()}_file_watch",
                trigger=triggers.LumisectionFileWatcherTrigger(
                    calibration, poll_interval=_WATCH_POLL_SECONDS, run_end_grace_seconds=_RUN_END_GRACE_SECONDS
                ),
            )
        ],
    )
    for calibration in CALIBRATIONS
}


# --- run primer DAG task -----------------------------------------------------------


def _latest_triggering_event_payload(context):
    """`extra` (unwrapped) of the most recent Asset event that triggered this
    DagRun -- the watcher's TriggerEvent payload lands under
    `extra["payload"]` (Trigger.submit_event wraps it)."""
    events_by_asset = context.get("triggering_asset_events") or {}
    all_events = [event for events in events_by_asset.values() for event in events]
    if not all_events:
        return {}

    def _ts(event):
        return getattr(event, "timestamp", None) or datetime.min.replace(tzinfo=timezone.utc)

    extra = dict(getattr(max(all_events, key=_ts), "extra", None) or {})
    if "run_number" not in extra and isinstance(extra.get("payload"), dict):
        extra = extra["payload"]
    return extra


def _prime_run(**context):
    payload = _latest_triggering_event_payload(context)
    announced_run = payload.get("run_number")

    created = []
    for calibration in CALIBRATIONS:
        latched = step2.find_new_run(calibration)  # UNCHANGED -- creates <cal>/run<N>/runStart.log, or None if present
        if latched is not None:
            created.append((calibration, latched))

    if created:
        logging.info(
            f"Primed run {announced_run}: created working dirs for "
            f"{', '.join(f'{c} run {r}' for c, r in created)} -- file watchers will pick them up"
        )
    else:
        logging.info(f"Run {announced_run} already primed for all calibrations (working dirs present) -- nothing to do")


# --- DAG assembly ----------------------------------------------------------------------


def _build_run_primer_dag():
    with DAG(
        dag_id=RUN_PRIMER_DAG_ID,
        description="Woken by Asset(ngt://runs): create the per-calibration working dirs so the file watchers can start",
        schedule=[RUNS_ASSET],
        start_date=pp.DAG_START_DATE,
        catchup=False,
        max_active_runs=1,
        default_args=pp.DEFAULT_ARGS,
        tags=["ngt", "watch", "run-primer"],
    ) as dag:
        PythonOperator(task_id="prime_run", python_callable=_prime_run)
    return dag


def _build_process_dag(calibration):
    with DAG(
        dag_id=_process_dag_id(calibration),
        description=f"Woken by Asset(ngt://files/{calibration.lower()}): run one {calibration} input file "
        "through Step 2, 3 and 4 (Step 4 over every file accumulated for the run so far)",
        schedule=[FILE_ASSETS[calibration]],
        start_date=pp.DAG_START_DATE,
        catchup=False,
        max_active_runs=10,
        default_args=pp.DEFAULT_ARGS,
        tags=["ngt", "watch", "process", calibration],
    ) as dag:
        step2_express = PythonOperator(
            task_id="step2_express", python_callable=pp.make_step2_express_callable(calibration), **pp.LAUNCH_RETRY_KWARGS
        )
        step3_alca = PythonOperator(
            task_id="step3_alca", python_callable=pp.make_step3_alca_callable(calibration), **pp.LAUNCH_RETRY_KWARGS
        )
        step4_harvest = PythonOperator(
            task_id="step4_harvest", python_callable=pp.make_step4_harvest_callable(calibration), **pp.LAUNCH_RETRY_KWARGS
        )
        step4_upload = PythonOperator(
            task_id="step4_upload", python_callable=pp.make_step4_upload_callable(calibration), **pp.UPLOAD_RETRY_KWARGS
        )
        step2_express >> step3_alca >> step4_harvest >> step4_upload
    return dag


globals()[RUN_PRIMER_DAG_ID] = _build_run_primer_dag()
for _calibration in CALIBRATIONS:
    globals()[_process_dag_id(_calibration)] = _build_process_dag(_calibration)
