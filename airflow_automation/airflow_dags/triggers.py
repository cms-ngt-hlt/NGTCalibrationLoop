"""Custom Airflow triggers for the Asset-driven NGT DAG design -- they run
inside Airflow's `triggerer` process, not a worker slot.

* RunWatcherTrigger / LumisectionFileWatcherTrigger (BaseEventTrigger) --
  *event-driven-scheduling* triggers, attached to a static `Asset` via
  `AssetWatcher` in ngt_dags_watch.py. They are NOT tied to any
  DagRun: the triggerer runs them continuously (while their consuming DAG is
  unpaused) and each `TriggerEvent` they yield updates the watched Asset,
  which triggers the DAG scheduled on it. See ngt_dags_watch.py's module
  docstring for that design, and watch_asset_scheduling_design.md §1 for the
  Asset/AssetWatcher machinery they plug into.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from airflow.sdk import Asset
from airflow.triggers.base import BaseEventTrigger, TriggerEvent

from ngt_calibration_loop import config, oms, step2

# Production default: 30 min. Overridable via _process_tasks.py's
# NGT_RUN_END_GRACE_SECONDS (airflow_automation/airflow_demo/airflow_demo.sh
# sets a short one for the live-test setup).
DEFAULT_RUN_END_GRACE_SECONDS = 30 * 60

# ngt_dags_watch.py's 4 static assets. Names are slugs (Airflow validates
# them); the human-facing identity is the URI. Kept here so the watcher
# triggers below and the DAG file agree on exactly one spelling.
_WATCH_CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")
RUNS_ASSET_NAME, RUNS_ASSET_URI = "ngt_runs", "ngt://runs"


def files_asset_name(calibration):
    return f"ngt_files_{calibration.lower()}"


def files_asset_uri(calibration):
    return f"ngt://files/{calibration.lower()}"


def _state_accessor(trigger, asset_name, asset_uri):
    """The AssetStateStoreAccessor for `asset_name`/`asset_uri`, or None if the
    triggerer didn't inject one (older Airflow, or the watched-asset name
    didn't line up). Callers fall back to an on-disk log in that case."""
    store = getattr(trigger, "asset_state_store", None)
    if store is None:
        return None
    try:
        return store[Asset(name=asset_name, uri=asset_uri)]
    except Exception:  # KeyError / TypeError / anything -- degrade to the disk fallback
        return None


def _watermark_get(accessor, key, fallback_path):
    if accessor is not None:
        try:
            return list(accessor.get(key, default=[]) or [])
        except Exception:
            pass
    if fallback_path is not None and fallback_path.exists():
        return [line.strip() for line in fallback_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return []


def _watermark_set(accessor, key, values, fallback_path):
    if accessor is not None:
        try:
            accessor.set(key, list(values))
            return
        except Exception:
            pass
    if fallback_path is not None:
        fallback_path.write_text("\n".join(str(v) for v in values) + "\n", encoding="utf-8")


def _watermark_delete(accessor, key, fallback_path):
    if accessor is not None:
        try:
            accessor.delete(key)
        except Exception:
            pass
    try:
        if fallback_path is not None and fallback_path.exists():
            fallback_path.unlink()
    except OSError:
        pass


class RunWatcherTrigger(BaseEventTrigger):
    """Watches OMS for a new PROTONS/collisions run and yields one
    ``TriggerEvent({"run_number", "run_start_time"})`` per genuinely-new run,
    updating ``Asset("ngt://runs")``.

    Calibration-agnostic on purpose: `filltype`/`l1hltmode` are identical
    across all three calibration YAMLs, so one `oms.find_new_run` query (using
    the first calibration's config) covers every calibration. It calls
    `oms.find_new_run` (pure query, no side effects) -- creating the per-
    calibration working directories is `ngt_watch_run_primer`'s job.

    Already-seen run numbers are remembered in this Asset's AssetStateStore
    (`seen_runs` key); if that store isn't available they fall back to
    ``DATA_BASE_PATH/_ngt_watch_runs_seen.log``. `oms.find_new_run`'s own
    "working dir already exists" check is a second line of defence once the
    primer has run.
    """

    def __init__(self, poll_interval: float = 5.0):
        super().__init__()
        self.poll_interval = poll_interval

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return ("airflow_dags.triggers.RunWatcherTrigger", {"poll_interval": self.poll_interval})

    def _find_run(self):
        ngt_params = config.load_ngt_parameters()
        calib_config = config.load_calibration_config(_WATCH_CALIBRATIONS[0])
        data_base_path = ngt_params.get("DATA_BASE_PATH", "/data/ngt")
        s2 = calib_config["step_2_config"]
        return oms.find_new_run(
            calib_config,
            data_base_path,
            _WATCH_CALIBRATIONS[0],
            max_latch_time_hours=s2.get("maxLatchTimeHours", step2.DEFAULT_MAX_LATCH_TIME_HOURS),
            min_ls_to_process=s2["minLsToProcess"],
        )

    def _seen_fallback_path(self):
        try:
            base = config.load_ngt_parameters().get("DATA_BASE_PATH", "/data/ngt")
            return Path(base) / "_ngt_watch_runs_seen.log"
        except Exception:
            return None

    async def run(self) -> AsyncIterator[TriggerEvent]:
        self.log.info("Watching OMS for a new run (Asset ngt://runs)")
        accessor = _state_accessor(self, RUNS_ASSET_NAME, RUNS_ASSET_URI)
        fallback = self._seen_fallback_path()
        while True:
            latched = await asyncio.to_thread(self._find_run)
            if latched is not None:
                seen = {str(x) for x in _watermark_get(accessor, "seen_runs", fallback)}
                if str(latched.run_number) not in seen:
                    _watermark_set(accessor, "seen_runs", sorted(seen | {str(latched.run_number)}), fallback)
                    self.log.info(f"New run {latched.run_number} -> updating Asset ngt://runs")
                    yield TriggerEvent(
                        {
                            "run_number": latched.run_number,
                            "run_start_time": latched.run_start_time.isoformat(),
                        }
                    )
                    return
            await asyncio.sleep(self.poll_interval)


class LumisectionFileWatcherTrigger(BaseEventTrigger):
    """Watches one calibration's currently-active run for new raw input files
    and yields one ``TriggerEvent({"run_number", "file"})`` **per file**,
    updating ``Asset("ngt://files/<calibration>")``.

    "Currently-active run" is read from disk -- the newest
    ``DATA_BASE_PATH/<calibration>/run*/`` directory that has ``runStart.log``
    and no ``runEnd.log`` (exactly `step2`'s own model). That directory is
    created by `ngt_watch_run_primer` when `Asset("ngt://runs")` updates, so a
    run-asset update is what "starts" this watcher doing work -- the watcher
    itself is always running, it's just idle until a run dir appears.

    Files already handed out are remembered in this Asset's AssetStateStore
    (`emitted_run<N>` key, dropped when the run finalizes); fallback is
    ``<working_dir>/_ngt_watch_emitted.log``. `step2.load_already_processed`
    is the other half of the dedup -- and `ngt_watch_process_<cal>`'s
    `step2_express` skips an already-processed file too.

    On `step2.check_ls_for_processing` returning `final`, or the run having
    been OMS-ended for `run_end_grace_seconds` (this trigger's own check, run-end
    relative rather than step2's run-start-relative `max_latch_time_hours`), it
    writes `runEnd.log` via `step2.finalize_cycle` and goes idle.

    **One event per run() invocation.** Airflow coalesces asset events that a
    watcher yields in the same instant into a single triggered DagRun, so
    yielding several files at once would drop all but one. This trigger yields
    at most one file, then returns; the triggerer auto-restarts it and the
    next scan hands out the next file (one poll_interval later).
    """

    def __init__(
        self,
        calibration: str,
        poll_interval: float = 5.0,
        run_end_grace_seconds: float = DEFAULT_RUN_END_GRACE_SECONDS,
    ):
        super().__init__()
        self.calibration = calibration
        self.poll_interval = poll_interval
        self.run_end_grace_seconds = run_end_grace_seconds

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "airflow_dags.triggers.LumisectionFileWatcherTrigger",
            {
                "calibration": self.calibration,
                "poll_interval": self.poll_interval,
                "run_end_grace_seconds": self.run_end_grace_seconds,
            },
        )

    def _calibration_dir(self):
        base = config.load_ngt_parameters().get("DATA_BASE_PATH", "/data/ngt")
        return Path(base) / self.calibration

    def _active_run(self):
        cal_dir = self._calibration_dir()
        if not cal_dir.exists():
            return None
        active = []
        for run_dir in cal_dir.glob("run*"):
            if (run_dir / step2.RUN_START_LOG_NAME).exists() and not (run_dir / step2.RUN_END_LOG_NAME).exists():
                try:
                    active.append(int(run_dir.name[len("run"):]))
                except ValueError:
                    continue
        return max(active) if active else None

    def _check(self, run_number):
        ctx = step2.build_run_context(self.calibration, run_number)
        return step2.check_ls_for_processing(ctx), ctx

    def _run_ended_grace_expired(self, run_number):
        end_time = oms.run_end_time(run_number)
        if end_time is None:
            return False
        return (datetime.now(timezone.utc) - end_time).total_seconds() >= self.run_end_grace_seconds

    def _finalize(self, ctx):
        step2.finalize_cycle(
            ctx, None, step2.CycleDecision(action="final", ls_to_process=set(), still_have_time=False)
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        self.log.info(f"Watching for new files (Asset ngt://files/{self.calibration.lower()})")
        accessor = _state_accessor(self, files_asset_name(self.calibration), files_asset_uri(self.calibration))
        while True:
            run_number = await asyncio.to_thread(self._active_run)
            if run_number is None:
                await asyncio.sleep(self.poll_interval)
                continue

            decision, ctx = await asyncio.to_thread(self._check, run_number)
            key = f"emitted_run{run_number}"
            fallback = Path(ctx.working_dir) / "_ngt_watch_emitted.log"

            if decision.action in ("batch", "final"):
                already = set(step2.load_already_processed(ctx.working_dir)) | set(
                    _watermark_get(accessor, key, fallback)
                )
                new = [s for s in sorted(str(p) for p in decision.ls_to_process) if s not in already]
                if new:
                    file_path = new[0]  # exactly one per invocation -- see the class docstring
                    _watermark_set(accessor, key, sorted(already | {file_path}), fallback)
                    self.log.info(f"[{self.calibration} run {run_number}] new file -> {file_path}")
                    yield TriggerEvent({"run_number": run_number, "file": file_path})
                    return  # restart -> next scan hands out the next file
                if decision.action == "final":
                    self.log.info(f"[{self.calibration} run {run_number}] check_ls_for_processing says final")
                    await asyncio.to_thread(self._finalize, ctx)
                    _watermark_delete(accessor, key, fallback)
                    return
                # batch decision but nothing new to hand out yet -- keep polling
            elif await asyncio.to_thread(self._run_ended_grace_expired, run_number):
                self.log.info(
                    f"[{self.calibration} run {run_number}] OMS-ended >= {self.run_end_grace_seconds}s ago "
                    "with nothing new -- writing runEnd.log, going idle"
                )
                await asyncio.to_thread(self._finalize, ctx)
                _watermark_delete(accessor, key, fallback)
                return

            await asyncio.sleep(self.poll_interval)
