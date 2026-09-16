"""Thin wrapper around Airflow 3's stable REST API (v2), used by the DAG files
in place of the internal-only mechanisms this pipeline used under Airflow 2
(`airflow.api.common.trigger_dag.trigger_dag`, and direct `DagRun`/
`TaskInstance` ORM queries via `airflow.utils.session.create_session`).

Why this exists: Airflow 3's task-execution model isolates task code from the
metadata database -- Airflow's own docs state plainly that direct DB/ORM
access from task code "will break in Airflow 3.2+", and even the built-in
`TriggerDagRunOperator` hit exactly this wall early in the 3.0 series (see
https://github.com/apache/airflow/issues/47499) before being fixed to go
through the same kind of API this module uses. `trigger_dag()` itself still
happens to work when called directly (verified empirically against 3.3.1 --
only the wrong `triggered_by` keyword needed fixing), but relying on that is
exactly the pattern upstream is deprecating, not a forward-compatible choice.
The REST API is the documented, supported way for task code to talk to
Airflow about *other* DAG runs -- see "Upgrading to Airflow 3" and the public
API docs.

This is also a natural seam for the "engine-agnostic live test setup" this
project is working towards (see scenario-player/sim_env.sh): everything Airflow-shaped
is isolated to this one small file, callable over plain HTTP, rather than
spread through the DAG files as direct internal-API/ORM calls.

Auth: the Simple Auth Manager's zero-credential admin-token endpoint
(`GET /auth/token`, enabled via `AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS
=True`, set in airflow_automation/airflow_demo/airflow_env.sh) is used rather than a real username/
password. That auth manager is explicitly documented as intended only for
development/testing -- exactly what this offline pipeline is -- so a
zero-credential admin token is an appropriate fit, not a shortcut that would
be inappropriate in production.
"""

import os
import time

import requests

API_BASE_URL = os.environ.get("NGT_AIRFLOW_API_BASE_URL", "http://localhost:8091")
_REQUEST_TIMEOUT = 15

_token_cache = {"token": None, "fetched_at": 0.0}
_TOKEN_REFRESH_SECONDS = 60 * 60  # tokens are valid ~24h; refetch well before that


def _token():
    """Return a cached admin JWT, fetching a fresh one if unset/stale.

    Cheap either way (tasks are short-lived, one process per task under
    LocalExecutor), but avoids a redundant /auth/token round trip for every
    single API call within one task's execution.
    """
    now = time.monotonic()
    if _token_cache["token"] is None or (now - _token_cache["fetched_at"]) > _TOKEN_REFRESH_SECONDS:
        response = requests.get(f"{API_BASE_URL}/auth/token", timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        _token_cache["token"] = response.json()["access_token"]
        _token_cache["fetched_at"] = now
    return _token_cache["token"]


def _get(path, **kwargs):
    response = requests.get(
        f"{API_BASE_URL}{path}", headers={"Authorization": f"Bearer {_token()}"}, timeout=_REQUEST_TIMEOUT, **kwargs
    )
    response.raise_for_status()
    return response.json()


def _post(path, json_body):
    response = requests.post(
        f"{API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        json=json_body,
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def trigger_dag_run(dag_id, run_id, conf):
    """POST a new DagRun for dag_id. `logical_date: None` is intentional --
    these are self-retriggering/handoff runs with no natural schedule
    interval, exactly the "manual run with no logical date" case Airflow 3
    made a first-class option for (see the "Upgrading to Airflow 3" notes on
    manual-run data_interval no longer being derived from logical_date).

    Returns the created DagRun's data, or None if a DagRun with this exact
    run_id already exists (Airflow rejects the duplicate with 409, treated
    here as an idempotent no-op rather than an error). ngt_dags_per_file.py
    relies on it: it dedupes per-file dispatch by giving each (calibration,
    run, file) a deterministic run_id instead of pre-checking DagRun history,
    so a file-detector cycle that (re-)discovers the same file is safe to just
    call this again."""
    try:
        return _post(f"/api/v2/dags/{dag_id}/dagRuns", {"dag_run_id": run_id, "conf": conf, "logical_date": None})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            return None
        raise
