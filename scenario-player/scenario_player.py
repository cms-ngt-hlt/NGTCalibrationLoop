#!/usr/bin/env python3
"""
Plays back a scripted timeline of LHC runs/lumisections against the fake
OMS/EOS state scenario-player/seed.py manages -- the "simulate a live run" half of the
live-test setup, kept deliberately separate from whichever orchestration
engine is watching it (airflow_automation/airflow_demo/airflow_demo.sh today; a future Prefect/Kestra/
REANA adapter would need none of this file changed).

A scenario file (YAML) describes one or more runs, each with a start offset
and a sequence of lumisections separated by delays, e.g.:

    runs:
      - calibrations: [EcalPedestals]   # every calibration this run's files feed
        run: 398600
        start_offset: 0        # seconds after playback starts
        minutes_ago: 2         # optional, forwarded to seed-run's start_time
        lumisections:
          - {ls: 51, after: 0}   # "after" = seconds since the previous event
          - {ls: 52, after: 20}  # in this run (previous LS, or run start)
          - {ls: 53, after: 20}
        end_after: 60           # seconds after the last LS (or run start)

`calibrations` is always a list, even for one calibration -- a run is a
single, CMS-wide DAQ run (there's only ever one active at a time across the
whole experiment, never two different run numbers at once -- build_timeline
rejects a scenario whose `runs` entries overlap in time for exactly that
reason), but its lumisections can feed *multiple* calibrations simultaneously,
since one run's RAW data underlies every calibration stream. Listing several
calibrations on one run plays the same start/LS-arrival/end timeline once per
calibration (sharing the one run number, each getting its own fake EOS files
-- see scenario-player/scenarios/concurrent_multi_calibration.yaml for a worked example),
which is the physically sensible way to model "multiple calibrations
processing concurrently" -- not two different run numbers overlapping in
time. See scenario-player/scenarios/*.yaml for more worked examples.

A scenario can also arm simulated OMS/cmsRun/upload_conditions.py failures at
scheduled points on the same timeline, via a top-level `faults:` list, e.g.:

    faults:
      - target: oms                # oms | cmsrun | upload_conditions
        mode: connection_error     # see faults.py
        run: 398600                # optional scope
        at: 15                     # seconds since playback start
        times: 2                   # default 1 (single-shot); "unlimited" to persist

      - target: cmsrun
        mode: exit_code
        exit_code: 139
        calibration: EcalPedestals  # REQUIRED for cmsrun/upload_conditions
        step: step2                 # optional further scope
        at: 40

See scenario-player/scenarios/fault_injection_demo.yaml for a full worked example, and
faults.py's module docstring for why this crosses into a
file under $NGT_DEV_HOME/faults/ (via scenario-player/seed.py's arm_fault()) rather than
an in-memory call: this process and Airflow's own scheduler/triggerer/worker
processes are different OS processes.

Usage:
  python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml
  python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml --speed 5
  python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml --dry-run

--speed scales every delay (2 = twice as fast, 0.5 = half speed); --dry-run
prints the resolved timeline and exits without touching $NGT_DEV_HOME or
sleeping, useful for validating a scenario file or wiring it into a
non-interactive test. This script only calls scenario-player/seed.py's library functions
(seed_run/add_ls/end_run) -- it has no Airflow (or any engine) import at all.
"""
import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import faults as ngt_faults  # noqa: E402  -- scenario-player/faults.py, the shared fault vocabulary
import seed  # noqa: E402  -- scenario-player/seed.py, reused as a library (engine-agnostic)


def _describe_fault(spec: ngt_faults.FaultSpec):
    scope_bits = [
        f"{k}={v}"
        for k, v in (("calibration", spec.calibration), ("run", spec.run), ("step", spec.step))
        if v is not None
    ]
    scope = f" ({', '.join(scope_bits)})" if scope_bits else ""
    return f"arm-fault --target {spec.target} --mode {spec.mode}{scope}"


@dataclass
class Event:
    at: float  # seconds since playback start
    kind: str  # "start_run" | "add_ls" | "end_run" | "fault"
    calibration: Optional[str] = None
    run: Optional[int] = None
    ls: Optional[int] = None
    minutes_ago: float = 2.0
    fault: Optional[ngt_faults.FaultSpec] = None

    def describe(self):
        if self.kind == "start_run":
            return f"seed-run --calibration {self.calibration} --run {self.run}"
        if self.kind == "add_ls":
            return f"add-ls --calibration {self.calibration} --run {self.run} --ls {self.ls}"
        if self.kind == "fault":
            return _describe_fault(self.fault)
        return f"end-run --run {self.run}"

    def fire(self):
        if self.kind == "start_run":
            seed.seed_run(self.calibration, self.run, minutes_ago=self.minutes_ago, ls=[])
        elif self.kind == "add_ls":
            seed.add_ls(self.calibration, self.run, self.ls)
        elif self.kind == "end_run":
            seed.end_run(self.run)
        elif self.kind == "fault":
            spec = self.fault
            seed.arm_fault(
                spec.target,
                spec.mode,
                status=spec.status,
                exit_code=spec.exit_code,
                calibration=spec.calibration,
                run=spec.run,
                step=spec.step,
                times=spec.times,
                message=spec.message,
            )
        else:
            raise ValueError(f"unknown event kind {self.kind!r}")


def _run_interval(spec):
    """The [start, end) window a run spec occupies on the timeline, ignoring
    which calibration(s) are involved -- used by _check_no_overlapping_runs."""
    t = float(spec.get("start_offset", 0))
    for ls_spec in spec.get("lumisections", []):
        t += float(ls_spec.get("after", 0))
    return float(spec.get("start_offset", 0)), t + float(spec.get("end_after", 0))


def _check_no_overlapping_runs(scenario):
    """A run is one CMS-wide DAQ run -- there is only ever one active across
    the whole of CMS at a time, never two different run numbers at once
    (unlike calibrations, which can all be watching the *same* run
    concurrently -- see build_timeline's docstring). Reject a scenario that
    schedules two different run numbers whose windows overlap, rather than
    silently producing a physically nonsensical timeline."""
    intervals = sorted(
        (*_run_interval(spec), spec["run"]) for spec in scenario.get("runs", [])
    )
    for (start1, end1, run1), (start2, _end2, run2) in zip(intervals, intervals[1:]):
        if start2 < end1:
            raise ValueError(
                f"scenario schedules run {run1} (active t=+{start1:.0f}s..+{end1:.0f}s) and run "
                f"{run2} (starting t=+{start2:.0f}s) overlapping in time -- a run is one CMS-wide "
                "DAQ run, there is only ever one active at once. If you want multiple calibrations "
                "watching the same run concurrently, list them together under that run's "
                "'calibrations' instead of splitting them into separate run entries."
            )


def build_timeline(scenario):
    """Turn a parsed scenario dict into a flat, time-sorted list of Events.

    Every (run, calibration) pair always gets its own start_run event with no
    LS attached (even if its first lumisection has after=0, i.e. "arrives
    immediately") -- that lumisection still becomes its own add_ls event
    fired at the same timestamp, rather than being folded into seed-run's own
    --ls option. This is a one-line-simpler timeline builder at the cost of
    one extra (still correctly-ordered) fake-OMS write per calibration; the
    resulting state is identical. end_run fires exactly once per run spec
    (not once per calibration) since scenario-player/seed.py's end-run isn't
    calibration-specific -- it just flips one shared OMS record.
    """
    _check_no_overlapping_runs(scenario)
    events = []
    for spec in scenario.get("runs", []):
        calibrations = spec.get("calibrations")
        if not calibrations or isinstance(calibrations, str):
            raise ValueError(
                f"run {spec.get('run')!r}: 'calibrations' must be a non-empty list (e.g. "
                "['EcalPedestals']), even for a single calibration -- not a bare string, and not omitted"
            )
        run = spec["run"]
        minutes_ago = float(spec.get("minutes_ago", 2))
        t = float(spec.get("start_offset", 0))
        for calibration in calibrations:
            events.append(Event(t, "start_run", calibration, run, minutes_ago=minutes_ago))
        for ls_spec in spec.get("lumisections", []):
            t += float(ls_spec.get("after", 0))
            for calibration in calibrations:
                events.append(Event(t, "add_ls", calibration, run, ls=ls_spec["ls"]))
        t += float(spec.get("end_after", 0))
        events.append(Event(t, "end_run", calibrations[0], run))

    for fault_entry in scenario.get("faults", []):
        fault_entry = dict(fault_entry)
        at = float(fault_entry.pop("at", 0))
        events.append(Event(at, "fault", fault=ngt_faults.parse_fault_spec(fault_entry)))

    events.sort(key=lambda e: e.at)
    return events


def play(events, speed=1.0, dry_run=False, log=print):
    if dry_run:
        for e in events:
            log(f"t=+{e.at:>7.1f}s  {e.describe()}")
        return

    t0 = time.monotonic()
    for e in events:
        target = t0 + e.at / speed
        remaining = target - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        stamp = datetime.now().strftime("%H:%M:%S")
        log(f"[{stamp}] +{e.at:.1f}s  {e.describe()}")
        e.fire()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", type=Path, help="path to a scenario YAML file")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier (default: 1.0)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resolved timeline and exit; touches nothing"
    )
    args = parser.parse_args()

    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    events = build_timeline(scenario)
    if not events:
        print("(scenario has no runs -- nothing to play)")
        return

    if not args.dry_run:
        print(f"Playing {args.scenario} at {args.speed}x speed ({len(events)} events, $NGT_DEV_HOME={seed.NGT_DEV_HOME})")
    play(events, speed=args.speed, dry_run=args.dry_run)
    if not args.dry_run:
        print("Scenario finished.")


if __name__ == "__main__":
    main()
