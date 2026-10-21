"""Airflow's side of `airflow_demo.sh play ... --monitor`: what the AssetWatcher design does, in
Airflow's own terms -- Asset events, the DagRuns they trigger, task retries -- next to the files
the scenario player's --monitor reports. `play` runs `follow` alongside the player and `summary`
after it. A dev script reading this checkout's metadata DB directly, like airflow_demo.sh's
_reset_* helpers; nothing here is task code.

    python3 watch_views.py follow <since>    report each new Asset event, DagRun state and task
                                              retry/failure as it happens, until stopped
    python3 watch_views.py summary <since>   what happened since then: DagRuns per DAG, retried
                                              tasks, Asset events, the watchers' watermarks

<since> is a UTC ISO-8601 timestamp (`date -u +%Y-%m-%dT%H:%M:%S`); activity before it is ignored.
Run with this checkout's airflow_env.sh sourced and the Airflow venv active.
"""

import json
import sys
import time
from datetime import datetime, timezone

from airflow.models import DagRun, TaskInstance
from airflow.models.asset import AssetEvent, AssetModel
from airflow.models.asset_state_store import AssetStateStoreModel
from airflow.utils.session import create_session
from sqlalchemy import or_

POLL_SECONDS = 2
DAGS_LIKE = "ngt_watch_%"
ASSETS_LIKE = ("ngt://runs%", "ngt://files/%")
RETRY_STATES = ("up_for_retry", "failed")


def _emit(lines):
    """One write per report, starting with a newline -- scenario_player.py's --monitor
    convention, so a report never lands on the tail of a pending --interactive prompt."""
    if lines:
        stamp = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write("\n".join(["", f"  [{stamp}] Airflow:"] + [f"    {line}" for line in lines]) + "\n")
        sys.stdout.flush()


def _payload(extra):
    """A watcher's TriggerEvent payload, which Trigger.submit_event nests under extra["payload"]."""
    extra = dict(extra or {})
    return extra["payload"] if isinstance(extra.get("payload"), dict) else extra


def _describe(payload):
    bits = []
    if "run_number" in payload:
        bits.append(f"run {payload['run_number']}")
    if payload.get("file"):
        bits.append(str(payload["file"]).rsplit("/", 1)[-1])
    return ", ".join(bits) or "no payload"


def _trigger_of(dag_run):
    """What woke a DagRun: the payloads of the Asset events it consumed."""
    return "; ".join(_describe(_payload(event.extra)) for event in dag_run.consumed_asset_events) or str(
        dag_run.run_type
    )


def _ngt_assets(session):
    return session.query(AssetModel).filter(or_(*(AssetModel.uri.like(p) for p in ASSETS_LIKE)))


def _events(session, since):
    return (
        session.query(AssetEvent, AssetModel.uri)
        .join(AssetModel, AssetModel.id == AssetEvent.asset_id)
        .filter(or_(*(AssetModel.uri.like(p) for p in ASSETS_LIKE)), AssetEvent.timestamp >= since)
        .order_by(AssetEvent.id)
    )


def _dag_runs(session, since):
    return (
        session.query(DagRun)
        .filter(DagRun.dag_id.like(DAGS_LIKE), DagRun.queued_at >= since)
        .order_by(DagRun.queued_at)
    )


def _task_instances(session, since):
    return (
        session.query(TaskInstance, DagRun)
        .join(DagRun, (TaskInstance.dag_id == DagRun.dag_id) & (TaskInstance.run_id == DagRun.run_id))
        .filter(DagRun.dag_id.like(DAGS_LIKE), DagRun.queued_at >= since)
    )


def _tries(ti):
    return f"try {ti.try_number} of {ti.max_tries + 1}"


def follow(since):
    """Poll until stopped, reporting changes only. A database that isn't there yet (`play` from a
    cold start creates it) is waited for, not reported."""
    seen_events, run_states, retried = set(), {}, {}
    while True:
        try:
            lines = []
            with create_session() as session:
                for event, uri in _events(session, since):
                    if event.id not in seen_events:
                        seen_events.add(event.id)
                        lines.append(f"Asset {uri.rstrip('/')} <- event: {_describe(_payload(event.extra))}")
                for dag_run in _dag_runs(session, since):
                    state = str(dag_run.state)
                    if run_states.get(dag_run.id) != state:
                        run_states[dag_run.id] = state
                        lines.append(f"DagRun {dag_run.dag_id} ({_trigger_of(dag_run)}): {state}")
                for ti, dag_run in _task_instances(session, since):
                    key, state = (ti.dag_id, ti.run_id, ti.task_id, ti.map_index), str(ti.state)
                    if state in RETRY_STATES and retried.get(key) != (state, ti.try_number):
                        retried[key] = (state, ti.try_number)
                        lines.append(f"task {ti.task_id} of {ti.dag_id} ({_trigger_of(dag_run)}): {state}, {_tries(ti)}")
                    elif state == "success" and key in retried and retried[key][0] != "success":
                        retried[key] = (state, ti.try_number)
                        lines.append(f"task {ti.task_id} of {ti.dag_id} ({_trigger_of(dag_run)}): success on {_tries(ti)}")
            _emit(lines)
        except Exception:  # not migrated / not reachable yet, or a transient error: try again
            pass
        time.sleep(POLL_SECONDS)


def _watermark(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list) and len(value) > 6:
        return f"{len(value)} entries, last: {', '.join(str(v).rsplit('/', 1)[-1] for v in value[-2:])}"
    if isinstance(value, list):
        return ", ".join(str(v).rsplit("/", 1)[-1] for v in value) or "(empty)"
    return str(value)


def summary(since):
    with create_session() as session:
        print(f"  Airflow, since {since:%H:%M:%S} UTC:")
        print("    DagRuns:")
        counts = {}
        for dag_run in _dag_runs(session, since):
            states = counts.setdefault(dag_run.dag_id, {})
            states[str(dag_run.state)] = states.get(str(dag_run.state), 0) + 1
        for dag_id, states in sorted(counts.items()):
            print(f"      {dag_id:34s} " + ", ".join(f"{n} {state}" for state, n in sorted(states.items())))
        if not counts:
            print("      (none)")

        retried = [
            (ti, dag_run) for ti, dag_run in _task_instances(session, since) if ti.try_number and ti.try_number > 1
        ]
        print("    Tasks that needed more than one try:" + ("" if retried else " none"))
        for ti, dag_run in retried:
            print(f"      {ti.dag_id}.{ti.task_id} ({_trigger_of(dag_run)}): {ti.state} on {_tries(ti)}")

        print("    Asset events:")
        events = {}
        for event, uri in _events(session, since):
            events.setdefault(uri, []).append(event)
        for uri, uri_events in sorted(events.items()):
            print(f"      {uri.rstrip('/'):28s} {len(uri_events):3d}  last: {_describe(_payload(uri_events[-1].extra))}")
        if not events:
            print("      (none)")

        print("    Watchers' watermarks (each Asset's AssetStateStore):")
        found = False
        for asset in _ngt_assets(session).order_by(AssetModel.uri):
            for row in session.query(AssetStateStoreModel).filter(AssetStateStoreModel.asset_id == asset.id):
                found = True
                print(f"      {asset.uri.rstrip('/'):28s} {row.key} = {_watermark(row.value)}")
        if not found:
            print("      (none -- a file watcher drops its emitted_run<N> once that run is finalized)")


def main(argv):
    if len(argv) != 3 or argv[1] not in ("follow", "summary"):
        sys.exit(__doc__)
    since = datetime.fromisoformat(argv[2])
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    try:
        (follow if argv[1] == "follow" else summary)(since)
    except KeyboardInterrupt:  # Ctrl-C at the walkthrough's prompt reaches this process too
        pass


if __name__ == "__main__":
    main(sys.argv)
