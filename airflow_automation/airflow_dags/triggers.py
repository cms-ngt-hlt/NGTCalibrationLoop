"""Custom Airflow trigger(s) for the per-file scheduling design
(ngt_dags_per_file.py) -- runs inside Airflow's `triggerer` process, not a
worker slot, which is the whole point of deferring: a file-detector DAG run
can wait for the next raw input file to appear for hours without occupying a
worker.

See ngt_dags_per_file.py's module docstring for how this fits into that
design overall.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from airflow.triggers.base import BaseTrigger, TriggerEvent

from ngt_calibration_loop import oms, step2

# Production default: 30 min. Overridable via ngt_dags_per_file.py's
# NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS (airflow_automation/airflow_demo/airflow_demo.sh sets a short
# one for the live-test setup, same pattern as NGT_LOOP_SLEEP_SECONDS).
DEFAULT_RUN_END_GRACE_SECONDS = 30 * 60


class NewFileTrigger(BaseTrigger):
    """Fires once there's at least one new raw input file for `calibration`'s
    `run_number` to process, once step2.check_ls_for_processing's own view
    says the run is done, or once the run ended a while ago and nothing new
    has shown up since (this trigger's own additional check -- see below).

    check_ls_for_processing's own give-up timer (max_latch_time_hours) is
    reused unchanged as the primary "is there something to do yet" oracle
    (see ngt_dags_per_file.py for why that's a safe, unmodified reuse) --
    but that timer is measured from run *start*, so a run that ends partway
    through its budget would otherwise leave this trigger deferred for
    whatever's left of it (e.g. a run that ran 3h of an 8h budget then ended
    would still wait out the remaining ~5h). This trigger additionally
    queries OMS directly (oms.run_end_time) each poll and stops waiting once
    the run has been over for `run_end_grace_seconds`, regardless of how
    much of check_ls_for_processing's own budget is left -- kept here, in
    the per-file design's own trigger, rather than as a change to step2.py's
    shared decision logic.

    The event payload is {"action": "batch"|"final", "files": [str, ...]} --
    `files` empty is possible on a "final" event (nothing new, but the run
    ended, either per check_ls_for_processing's own view or this trigger's
    grace period).
    """

    def __init__(
        self,
        calibration: str,
        run_number: int,
        poll_interval: float = 5.0,
        run_end_grace_seconds: float = DEFAULT_RUN_END_GRACE_SECONDS,
    ):
        super().__init__()
        self.calibration = calibration
        self.run_number = run_number
        self.poll_interval = poll_interval
        self.run_end_grace_seconds = run_end_grace_seconds

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "airflow_dags.triggers.NewFileTrigger",
            {
                "calibration": self.calibration,
                "run_number": self.run_number,
                "poll_interval": self.poll_interval,
                "run_end_grace_seconds": self.run_end_grace_seconds,
            },
        )

    def _check_once(self):
        ctx = step2.build_run_context(self.calibration, self.run_number)
        return step2.check_ls_for_processing(ctx)

    def _run_ended_grace_expired(self):
        """True once oms.run_end_time reports the run ended
        >= run_end_grace_seconds ago. None (still running, not found, or OMS
        unreachable) never expires the grace period -- same "don't give up
        on an ambiguous/unreachable OMS" caution check_ls_for_processing's
        own logic already takes."""
        end_time = oms.run_end_time(self.run_number)
        if end_time is None:
            return False
        return (datetime.now(timezone.utc) - end_time).total_seconds() >= self.run_end_grace_seconds

    async def run(self) -> AsyncIterator[TriggerEvent]:
        """Poll in a thread (not directly on the event loop): both checks
        below do blocking file I/O and/or an OMS HTTP call, and the
        triggerer process multiplexes many deferred tasks' triggers on one
        event loop -- a slow OMS response here would otherwise stall every
        other calibration/run currently deferred alongside this one, not
        just this trigger."""
        self.log.info(f"Deferred: watching for new files ({self.calibration} run {self.run_number})")
        while True:
            decision = await asyncio.to_thread(self._check_once)
            if decision.action in ("batch", "final"):
                yield TriggerEvent(
                    {"action": decision.action, "files": sorted(str(p) for p in decision.ls_to_process)}
                )
                return

            if await asyncio.to_thread(self._run_ended_grace_expired):
                self.log.info(
                    f"Run {self.run_number} ended >= {self.run_end_grace_seconds}s ago with nothing new "
                    f"for {self.calibration} -- giving up rather than waiting out check_ls_for_processing's "
                    "own (run-start-relative) timeout"
                )
                yield TriggerEvent({"action": "final", "files": sorted(str(p) for p in decision.ls_to_process)})
                return

            await asyncio.sleep(self.poll_interval)
