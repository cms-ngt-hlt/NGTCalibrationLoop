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
  by an event -- they're just idle until a run dir appears). Retried on failure
  (_process_tasks.PRIME_RETRY_KWARGS): find_new_run raises during an OMS outage,
  and the run watcher won't announce the same run twice.

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
import os
from datetime import datetime, timezone

import yaml
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Asset, AssetWatcher

from airflow_dags import _process_tasks as pp
from airflow_dags import triggers
from ngt_calibration_loop import run_step, step2

CALIBRATIONS = pp.CALIBRATIONS

RUN_PRIMER_DAG_ID = "ngt_watch_run_primer"

# Env-overridable poll/grace knobs (airflow_automation/airflow_demo/airflow_demo.sh
# sets short values for the live-test setup).
_WATCH_POLL_SECONDS = pp.FILE_POLL_INTERVAL_SECONDS
_RUN_END_GRACE_SECONDS = pp.RUN_END_GRACE_SECONDS

# Steps 2, 3 and 4's harvest run in the worker, as the library's step runner, unless the deployment gives a pod
# template: the YAML of a Pod, in NGT_STEP_POD_TEMPLATE (multipod-helm's values-step-pods.yaml). Then each runs in a
# pod of its own built from it (KubernetesPodOperator), as the step runner's command line: in another image, such as
# a CMSSW one, if the template says so. Read when the DAGs are parsed, so a change takes a restart.
STEP_POD_TEMPLATE = yaml.safe_load(os.environ.get("NGT_STEP_POD_TEMPLATE") or "null")


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
        PythonOperator(task_id="prime_run", python_callable=_prime_run, **pp.PRIME_RETRY_KWARGS)
    return dag


def _run_and_file(dag_run, triggering_asset_events):
    """The run and file this DAG run is for, as the step pods' arguments: what the PythonOperators read themselves."""
    return pp.run_and_file_from_context({"dag_run": dag_run, "triggering_asset_events": triggering_asset_events})


def _step_task(calibration, task_id, make_callable, **kwargs):
    if STEP_POD_TEMPLATE is None:
        return PythonOperator(
            task_id=task_id, python_callable=make_callable(calibration), **pp.LAUNCH_RETRY_KWARGS, **kwargs
        )

    # Only where pods are wanted: elsewhere the image needn't have Airflow's Kubernetes provider.
    from airflow.providers.cncf.kubernetes.operators.pod import (  # pylint: disable=import-outside-toplevel
        KubernetesPodOperator,
    )

    return KubernetesPodOperator(
        task_id=task_id,
        pod_template_dict=STEP_POD_TEMPLATE,
        in_cluster=True,
        # The template's container runs `python3 -m ngt_calibration_loop.run_step "$@"` in whatever environment it
        # sets up; these are the "$@".
        arguments=[
            task_id,
            "--calibration",
            calibration,
            "--run",
            "{{ ngt_run_and_file(dag_run, triggering_asset_events)[0] }}",
            "--file",
            "{{ ngt_run_and_file(dag_run, triggering_asset_events)[1] }}",
        ],
        skip_on_exit_code=run_step.EXIT_NOTHING_TO_DO,
        get_logs=True,
        on_finish_action="delete_succeeded_pod",  # a failed one stays, to look at
        startup_timeout_seconds=600,  # a first pull of a CMSSW image, or a node to free up
        **pp.LAUNCH_RETRY_KWARGS,
        **kwargs,
    )


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
        user_defined_macros={"ngt_run_and_file": _run_and_file},
    ) as dag:
        step2_express = _step_task(calibration, "step2_express", pp.make_step2_express_callable)
        step3_alca = _step_task(calibration, "step3_alca", pp.make_step3_alca_callable)
        # One harvest at a time per calibration: two that see the same Step 3 outputs share a job directory (step4.py
        # names it after them), and at once they'd write the same payload file. One file per lumisection ends several
        # Step 3s within seconds of each other (daq-dev, 2026-11-15).
        step4_harvest = _step_task(
            calibration, "step4_harvest", pp.make_step4_harvest_callable, max_active_tis_per_dag=1
        )
        # The fake conditions DB is the Airflow image's, and a real upload goes nowhere from a cluster: in the worker.
        step4_upload = PythonOperator(
            task_id="step4_upload", python_callable=pp.make_step4_upload_callable(calibration), **pp.UPLOAD_RETRY_KWARGS
        )
        step2_express >> step3_alca >> step4_harvest >> step4_upload
    return dag


globals()[RUN_PRIMER_DAG_ID] = _build_run_primer_dag()
for _calibration in CALIBRATIONS:
    globals()[_process_dag_id(_calibration)] = _build_process_dag(_calibration)
