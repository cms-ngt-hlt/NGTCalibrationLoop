"""Airflow DAGs for the NGT calibration loop.

For each (calibration, step) pair this file builds two DAGs:

- ngt_step{N}_{calibration}_latch: cron-scheduled (every 30s), looks for a new
  LHC run to latch onto (same logic as the old FSM's NewRunAvailable /
  NewRunAppeared) and, when found, triggers the processor DAG for it.

- ngt_step{N}_{calibration}_process: schedule=None, triggered only with
  {"run_number": ..., "cycle": ...} conf. Each DAG run does exactly one
  check-batch-launch cycle (mirroring the old FSM's WaitingForLS ->
  CheckingLSForProcess -> Preparing* -> Launching* -> Cleanup loop), then
  either retriggers itself for the next cycle of the same run, or -- once the
  run is finished -- triggers the next step's processor DAG. Step 4 has no
  next step.

Every task that actually launches a CMSSW job or uploads to condDB
(launch_job, and Step 4's upload_conditions) runs synchronously and is
retried/timed-out by Airflow -- see ngt_calibration_loop.shell.run_job_script
and the plan doc's "Key semantic changes" section for why this is safe.

See the plan at the top of this migration for the full design rationale.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from airflow import DAG
from airflow.api.common.trigger_dag import trigger_dag
from airflow.exceptions import AirflowSkipException
from airflow.models import DagRun
from airflow.operators.python import PythonOperator
from airflow.utils import timezone as af_timezone
from airflow.utils.session import create_session
from airflow.utils.trigger_rule import TriggerRule

from ngt_calibration_loop import step2, step3, step4

CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")
STEP_LIBS = {2: step2, 3: step3, 4: step4}

LATCH_SCHEDULE = timedelta(seconds=30)
CYCLE_SLEEP_SECONDS = int(os.environ.get("NGT_LOOP_SLEEP_SECONDS", 60))
DAG_START_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
DEFAULT_ARGS = {"owner": "ngt-calibration"}


# --- naming / lookup helpers --------------------------------------------------------


def _latch_dag_id(step_num, calibration):
    return f"ngt_step{step_num}_{calibration.lower()}_latch"


def _process_dag_id(step_num, calibration):
    return f"ngt_step{step_num}_{calibration.lower()}_process"


def _build_ctx(step_num, calibration, run_number):
    lib = STEP_LIBS[step_num]
    return lib.build_run_context(calibration, int(run_number) if step_num == 2 else str(run_number))


def _check(step_num, lib, ctx):
    return lib.check_ls_for_processing(ctx) if step_num == 2 else lib.check_files_for_processing(ctx)


def _decision_batch(step_num, decision):
    return decision.ls_to_process if step_num == 2 else decision.files_to_process


def _prepare(step_num, lib, ctx, batch):
    if step_num == 2:
        return lib.prepare_express_job(ctx, batch)
    if step_num == 3:
        return lib.prepare_alca_prompt_job(ctx, batch)
    return lib.prepare_harvesting_job(ctx, batch)


def _run(step_num, lib, job_spec):
    if step_num == 2:
        return lib.run_express_job(job_spec)
    if step_num == 3:
        return lib.run_alca_prompt_job(job_spec)
    return lib.run_harvesting_job(job_spec)


def _deserialize_batch(step_num, items):
    return set(items) if step_num == 2 else {Path(p) for p in items}


def _make_decision(step_num, action, batch):
    lib = STEP_LIBS[step_num]
    if step_num == 2:
        return lib.CycleDecision(action=action, ls_to_process=batch, still_have_time=(action != "final"))
    return lib.CycleDecision(action=action, files_to_process=batch)


def _existing_run_numbers(process_dag_id):
    """Run numbers already latched by this processor DAG, sourced from
    Airflow's own DagRun history -- replaces the old in-memory
    setOfRunsProcessed dedup set (see the plan's DAG design section)."""
    run_numbers = set()
    with create_session() as session:
        for dag_run in session.query(DagRun).filter(DagRun.dag_id == process_dag_id).all():
            conf = dag_run.conf or {}
            if "run_number" in conf:
                run_numbers.add(str(conf["run_number"]))
    return run_numbers


def _trigger_process_dag(step_num, calibration, run_number, cycle):
    process_dag_id = _process_dag_id(step_num, calibration)
    stamp = af_timezone.utcnow().strftime("%Y%m%dT%H%M%S%f")
    run_id = f"run{run_number}__cycle{cycle}__{stamp}"
    trigger_dag(dag_id=process_dag_id, run_id=run_id, conf={"run_number": str(run_number), "cycle": cycle})
    logging.info(f"Triggered {process_dag_id} run_id={run_id} (run {run_number}, cycle {cycle})")


# --- latch DAG task -------------------------------------------------------------------


def _make_latch_callable(step_num, calibration):
    lib = STEP_LIBS[step_num]

    def _latch(**_context):
        if step_num == 2:
            run_number = lib.find_new_run(calibration)
        else:
            already = _existing_run_numbers(_process_dag_id(step_num, calibration))
            run_number = lib.find_new_run(calibration, already_latched_run_numbers=already)

        if run_number is None:
            raise AirflowSkipException("No new run to latch onto this tick")

        _trigger_process_dag(step_num, calibration, run_number, cycle=0)

    return _latch


# --- process DAG tasks -----------------------------------------------------------------


def _make_check_and_batch_callable(step_num, calibration):
    lib = STEP_LIBS[step_num]

    def _check_and_batch(**context):
        run_number = context["dag_run"].conf["run_number"]
        ctx = _build_ctx(step_num, calibration, run_number)
        decision = _check(step_num, lib, ctx)
        batch = _decision_batch(step_num, decision)

        ti = context["ti"]
        ti.xcom_push(key="action", value=decision.action)
        ti.xcom_push(key="batch", value=sorted(str(x) for x in batch))
        logging.info(
            f"[{calibration} step{step_num} run {run_number}] action={decision.action} batch_size={len(batch)}"
        )

    return _check_and_batch


def _pull_action_and_batch(step_num, context):
    ti = context["ti"]
    action = ti.xcom_pull(task_ids="check_and_batch", key="action")
    items = ti.xcom_pull(task_ids="check_and_batch", key="batch") or []
    return action, _deserialize_batch(step_num, items)


def _make_prepare_job_callable(step_num, calibration):
    lib = STEP_LIBS[step_num]

    def _prepare_job(**context):
        action, batch = _pull_action_and_batch(step_num, context)
        if action == "wait":
            raise AirflowSkipException("Nothing new this cycle")

        run_number = context["dag_run"].conf["run_number"]
        ctx = _build_ctx(step_num, calibration, run_number)
        job_spec = _prepare(step_num, lib, ctx, batch)
        if job_spec is None:
            raise AirflowSkipException("Nothing to process this cycle (batch empty after validation)")
        logging.info(f"Prepared job dir: {getattr(job_spec, 'job_dir', getattr(job_spec, 'working_dir', '?'))}")

    return _prepare_job


def _make_launch_job_callable(step_num, calibration):
    """Re-derives and re-prepares the job (idempotent/deterministic -- see the
    plan's job-dir-naming note) rather than trying to pass the JobSpec through
    XCom, then blocks on running it. This is the actual fix: the old code used
    subprocess.Popen specifically to avoid blocking the FSM's loop; an Airflow
    task already owns its own worker slot, so it can just block and let
    Airflow's retries/timeout/UI history observe the real outcome."""
    lib = STEP_LIBS[step_num]

    def _launch_job(**context):
        action, batch = _pull_action_and_batch(step_num, context)
        if action == "wait":
            raise AirflowSkipException("Nothing new this cycle")

        run_number = context["dag_run"].conf["run_number"]
        ctx = _build_ctx(step_num, calibration, run_number)
        job_spec = _prepare(step_num, lib, ctx, batch)
        if job_spec is None:
            raise AirflowSkipException("Nothing to launch this cycle")

        _run(step_num, lib, job_spec)

    return _launch_job


def _make_upload_conditions_callable(calibration):
    def _upload(**context):
        action, batch = _pull_action_and_batch(4, context)
        if action == "wait":
            raise AirflowSkipException("Nothing new this cycle")

        run_number = context["dag_run"].conf["run_number"]
        ctx = _build_ctx(4, calibration, run_number)
        job_spec = step4.prepare_harvesting_job(ctx, batch)
        if job_spec is None:
            raise AirflowSkipException("Nothing to upload this cycle")

        step4.upload_conditions(job_spec)

    return _upload


def _make_finalize_callable(step_num, calibration):
    lib = STEP_LIBS[step_num]

    def _finalize(**context):
        action, batch = _pull_action_and_batch(step_num, context)
        run_number = context["dag_run"].conf["run_number"]
        ctx = _build_ctx(step_num, calibration, run_number)
        decision = _make_decision(step_num, action, batch)

        job_spec = _prepare(step_num, lib, ctx, batch) if action != "wait" else None
        is_final = lib.finalize_cycle(ctx, job_spec, decision)

        context["ti"].xcom_push(key="is_final", value=is_final)
        logging.info(f"[{calibration} step{step_num} run {run_number}] cycle finalized, is_final={is_final}")

    return _finalize


def _make_advance_callable(step_num, calibration):
    def _advance(**context):
        conf = context["dag_run"].conf
        run_number = conf["run_number"]
        cycle = conf.get("cycle", 0)
        is_final = context["ti"].xcom_pull(task_ids="finalize_cycle", key="is_final")

        if not is_final:
            time.sleep(CYCLE_SLEEP_SECONDS)
            _trigger_process_dag(step_num, calibration, run_number, cycle=cycle + 1)
            return

        logging.info(f"Run {run_number} finished Step {step_num} processing for {calibration}")
        if step_num < 4:
            _trigger_process_dag(step_num + 1, calibration, run_number, cycle=0)

    return _advance


# --- DAG assembly -----------------------------------------------------------------------


def _build_latch_dag(step_num, calibration):
    with DAG(
        dag_id=_latch_dag_id(step_num, calibration),
        description=f"Latch new runs onto Step {step_num} processing for {calibration}",
        schedule=LATCH_SCHEDULE,
        start_date=DAG_START_DATE,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULT_ARGS,
        tags=["ngt", f"step{step_num}", calibration, "latch"],
    ) as dag:
        PythonOperator(task_id="find_and_trigger", python_callable=_make_latch_callable(step_num, calibration))
    return dag


def _build_process_dag(step_num, calibration):
    with DAG(
        dag_id=_process_dag_id(step_num, calibration),
        description=f"Process one LHC run through Step {step_num} for {calibration}, one cycle per DAG run",
        schedule=None,
        start_date=DAG_START_DATE,
        catchup=False,
        max_active_runs=10,  # multiple runs' chains may be in flight concurrently
        default_args=DEFAULT_ARGS,
        tags=["ngt", f"step{step_num}", calibration, "process"],
    ) as dag:
        check_and_batch = PythonOperator(
            task_id="check_and_batch",
            python_callable=_make_check_and_batch_callable(step_num, calibration),
        )
        prepare_job = PythonOperator(
            task_id="prepare_job",
            python_callable=_make_prepare_job_callable(step_num, calibration),
        )
        launch_job = PythonOperator(
            task_id="launch_job",
            python_callable=_make_launch_job_callable(step_num, calibration),
            retries=3,
            retry_delay=timedelta(minutes=2),
            retry_exponential_backoff=True,
            execution_timeout=timedelta(hours=2),
        )

        chain = [check_and_batch, prepare_job, launch_job]

        if step_num == 4:
            upload_conditions_task = PythonOperator(
                task_id="upload_conditions",
                python_callable=_make_upload_conditions_callable(calibration),
                retries=5,
                retry_delay=timedelta(minutes=1),
                retry_exponential_backoff=True,
                execution_timeout=timedelta(minutes=30),
            )
            chain.append(upload_conditions_task)

        finalize_cycle = PythonOperator(
            task_id="finalize_cycle",
            python_callable=_make_finalize_callable(step_num, calibration),
            trigger_rule=TriggerRule.NONE_FAILED,
        )
        advance = PythonOperator(
            task_id="advance",
            python_callable=_make_advance_callable(step_num, calibration),
            trigger_rule=TriggerRule.NONE_FAILED,
        )
        chain += [finalize_cycle, advance]

        for upstream, downstream in zip(chain, chain[1:]):
            upstream >> downstream
    return dag


for _calibration in CALIBRATIONS:
    for _step_num in (2, 3, 4):
        globals()[_latch_dag_id(_step_num, _calibration)] = _build_latch_dag(_step_num, _calibration)
        globals()[_process_dag_id(_step_num, _calibration)] = _build_process_dag(_step_num, _calibration)
