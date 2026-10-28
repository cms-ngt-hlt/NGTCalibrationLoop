#!/usr/bin/env python3
"""
Plays back a scripted timeline of LHC runs/lumisections against the fake
OMS/EOS state scenario-player/seed.py manages -- the "simulate a live run" half of the
live-test setup. It doesn't know or care what, if anything, is watching
$NGT_DEV_HOME.

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

A top-level `faults:` list arms simulated OMS/cmsRun/upload_conditions.py failures on the
same timeline: each entry is a faults.py fault plus `at`, seconds since playback start (see
scenario-player/scenarios/fault_injection_demo.yaml).

A top-level `expect:` block lists the payloads that should reach the dummy conditions DB (see
scenario-player/expectations.py). After playback the player waits for this play's uploads to
settle and checks them, so a scenario passes or fails by itself:

    expect:
      allow_extra: {before: true}      # tolerate the intermediate re-harvests
      payloads:
        - {calibration: EcalPedestals, run: 398600, lumisections: 51-53}

Exit codes: 0 = played, and the expectations (if any) held; 1 = they didn't, or the uploads never
settled; 2 = a malformed scenario, or --interactive ran out of stdin; 130 = Ctrl-C.

This script only calls scenario-player/seed.py's library functions
(seed_run/add_ls/end_run/arm_fault) and reads the ledger via
scenario-player/fake_conddb.py -- it has no orchestration-engine import at all.
"""
# see def parse_args below to see available commands
import argparse
import itertools
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import expectations as ngt_expectations  # noqa: E402  -- scenario-player/expectations.py
import fake_conddb  # noqa: E402  -- scenario-player/fake_conddb.py, the dummy conditions DB
import faults as ngt_faults  # noqa: E402  -- scenario-player/faults.py, the shared fault vocabulary
import seed  # noqa: E402  -- scenario-player/seed.py, reused as a library (engine-agnostic)

MONITOR_INTERVAL_SECONDS = 2.0  # fast enough to feel live, slow enough to stay quiet

EXIT_OK = 0
EXIT_EXPECTATIONS_FAILED = 1
EXIT_BAD_SCENARIO = 2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", type=Path, help="path to a scenario YAML file")
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="playback speed multiplier -- 2 = twice as fast, 0.5 = half speed "
             "(default: 1.0; not usable with --interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved timeline and expectations and exit; touches nothing (useful for "
             "validating a scenario file or wiring it into a non-interactive test)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="skip playback and check the whole conditions DB against the scenario's expect: block",
    )
    parser.add_argument(
        "--no-validate", action="store_true", help="play the scenario but ignore its expect: block"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="wait for Enter before each scheduled instant (printing the seed.py command(s) about "
             "to run) instead of waiting out its delay; does not start the loops being watched -- "
             "see live_demo.sh's `play`",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="report files appearing under $NGT_DEV_HOME/data while the scenario plays (what Step "
             "2/3/4 produce in reaction, not this script), and keep watching after the last event "
             "until you press Enter; combined with --interactive, lets you confirm each step once "
             "its real output has appeared instead of guessing",
    )
    parser.add_argument(
        "--only-list-calibrations",
        action="store_true",
        help="ignore every other argument: print the calibrations this scenario's runs "
             "feed, one per line, and exit (what live_demo.sh's `play` uses to know which "
             "loops to start)",
    )
    args = parser.parse_args()

    if not args.only_list_calibrations:
        # Checked before the scenario file is read, so a contradictory command line fails
        # the same way whether or not the path exists.
        if args.interactive and args.dry_run:
            parser.error(
                "--interactive and --dry-run are mutually exclusive: --dry-run only prints the resolved "
                "timeline, there is nothing to confirm"
            )
        if args.interactive and args.speed is not None:
            parser.error("--speed has no meaning with --interactive: nothing is timed, every step waits for you")
        if args.monitor and args.dry_run:
            parser.error(
                "--monitor has no meaning with --dry-run: nothing is fired, so nothing will appear under "
                f"{seed.NGT_DEV_HOME / 'data'}"
            )
        if args.speed is not None and args.speed <= 0:
            parser.error(f"--speed must be positive, got {args.speed}")
        if args.validate_only and args.no_validate:
            parser.error("--validate-only and --no-validate are mutually exclusive")
        if args.validate_only and (args.interactive or args.dry_run or args.monitor or args.speed is not None):
            parser.error(
                "--validate-only plays nothing: --interactive, --dry-run, --monitor and --speed have no "
                "meaning with it"
            )
    return args


def _emit(message):
    """Write one whole message -- possibly several lines -- in a single call.

    The flush is for prompts, which have to be visible before input() blocks --
    including when stdout is a pipe into `tee`. (print() would be two writes: the text,
    then the newline.)
    """
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _load_scenario(path):
    """The one way this script reads a scenario file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _describe_fault(spec: ngt_faults.FaultSpec):
    scope = ", ".join(f"{k}={getattr(spec, k)}" for k in ("calibration", "run", "step") if getattr(spec, k) is not None)
    return f"arm-fault --target {spec.target} --mode {spec.mode}" + (f" ({scope})" if scope else "")


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
            seed.arm_fault(self.fault)
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
    """Reject a scenario whose runs overlap in time -- a run is one
    CMS-wide DAQ run, never two active at once (see module docstring)."""
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


def show_timeline(events, log=_emit):
    """--dry-run: the resolved timeline, printed rather than played."""
    for e in events:
        log(f"t=+{e.at:>7.1f}s  {e.describe()}")


def _prompt_text(number, total, instant):
    """The --interactive prompt for one instant: what is about to happen, as the
    equivalent scenario-player/seed.py command lines, and how far through the scenario
    we are."""
    return "\n".join(
        [f"[{number}/{total}] next, scheduled +{instant[0].at:.1f}s -- {len(instant)} event(s):"]
        + [f"         {e.describe()}" for e in instant]
        + [">>> Press Enter to fire (Ctrl-C to stop here)... "]
    )


def play(events, speed=1.0, log=_emit, prompt=None):
    """Fire every event on the timeline, in order.

    prompt=None: wait out each event's scheduled offset, scaled by `speed`.
    prompt=<callable>: ignore the clock and ask it to confirm each instant instead -- the
    --interactive mode, where whoever is watching sets the pace. Any callable taking a
    string will do (input, in practice), so tests need no terminal; EOFError and
    KeyboardInterrupt out of it propagate untouched for main() to interpret. Offsets are
    still printed, since the scripted timing is worth seeing even when it isn't honored.

    Events scheduled at the same offset are confirmed together: build_timeline emits one
    start_run/add_ls *per calibration*, so a run feeding three calibrations would
    otherwise want three near-identical keypresses (scenarios/sequential_runs.yaml: 10
    instants, not 32). Grouping is safe because build_timeline sorts stably by .at -- it
    never reorders or drops anything, and timed playback can't tell the difference (one
    sleep, then the whole instant back to back, exactly as before).
    """
    instants = [list(group) for _at, group in itertools.groupby(events, key=lambda e: e.at)]
    t0 = time.monotonic()
    for number, instant in enumerate(instants, start=1):
        if prompt is None:
            remaining = (t0 + instant[0].at / speed) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        else:
            prompt(_prompt_text(number, len(instants), instant))
        for e in instant:
            log(f"[{datetime.now().strftime('%H:%M:%S')}] +{e.at:.1f}s  {e.describe()}")
            e.fire()


class DataDirMonitor:
    """Reports files/directories appearing under the demo-env data directory while a
    scenario plays -- i.e. whatever the loops under test are producing. This player writes
    none of it (it only touches the fake OMS/EOS state scenario-player/seed.py owns), so
    every path reported here is something NGTLoopStep2/3/4 did in reaction.

    Creations only: a path is reported the first time a scan sees it, never again for
    appends or growth (watching a log grow is `live_demo.sh logs`' job). Constructing the
    monitor baselines whatever is already there and reports none of it, so a demo-env left
    over from an earlier scenario doesn't dump its whole tree at t=0. Deletions are
    silent, but they do drop out of the baseline -- so ALCAOUTPUT.sh self-deleting and
    being regenerated is reported again, which is the useful behavior.
    """

    def __init__(self, root, log=_emit, interval=MONITOR_INTERVAL_SECONDS):
        self.root = Path(root)
        self.log = log
        self.interval = interval
        self._seen = self._scan()
        self._stop = threading.Event()
        self._thread = None

    def _scan(self):
        """{path: is_a_directory} for everything under self.root right now.

        A missing root isn't an error, just an empty scan (os.walk yields nothing and
        raises nothing): $NGT_DEV_HOME/data is created by sim_env.sh's sim_setup, and this
        player deliberately never creates it -- doing so would quietly mask a forgotten
        `live_demo.sh setup`. Symlinks aren't followed (os.walk's default).
        """
        found = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            for name in dirnames:
                found[here / name] = True
            for name in filenames:
                found[here / name] = False
        return found

    def _describe(self, path, is_directory):
        """One reported path: relative to the data directory's parent ($NGT_DEV_HOME),
        posix slashes, a trailing slash for a directory (a new alcaPromptJob000/ is a
        headline) and a byte count for a file -- dropped if it vanished again in
        between."""
        shown = path.relative_to(self.root.parent).as_posix()
        if is_directory:
            return shown + "/"
        try:
            return f"{shown}  ({path.stat().st_size} B)"
        except OSError:
            return shown

    def poll(self):
        """Report whatever appeared since the last poll (or since construction), keeping
        this scan as the new baseline. Returns the reported lines.

        Sorting the *described* paths is what puts parents before children, since a
        parent's line is a prefix of its children's. Called from the watcher thread, and
        once more by stop() after that thread has been joined -- never from two threads at
        once, which is why self._seen needs no lock.
        """
        current = self._scan()
        new = sorted(
            self._describe(path, is_directory)
            for path, is_directory in current.items()
            if path not in self._seen
        )
        self._seen = current
        if new:
            # One log() call for the whole block, never one per line: the watcher thread
            # must not be able to tear its own report in half around a main-thread write.
            # The leading newline is what keeps a report from landing on the tail of an
            # --interactive prompt, which has deliberately not been terminated yet; with
            # no prompt pending it just spaces consecutive reports out.
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log(
                "\n".join(
                    ["", f"  [{stamp}] {len(new)} new path(s) under $NGT_DEV_HOME:"]
                    + [f"    + {line}" for line in new]
                )
            )
        return new

    def start(self):
        """Poll in the background until stop().

        A thread rather than polling between events because --monitor has to keep printing
        while the main thread is blocked in input() -- both for --interactive's per-step
        prompt and for the wait after the last event. daemon=True is only the safety net
        for an exit path that never reaches stop() (an unexpected traceback); stop() is the
        normal way out, and the one that preserves the last report.
        """
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="ngt-data-monitor", daemon=True)
        self._thread.start()

    def _watch(self):
        # wait() returns as soon as stop() sets the event, so stopping is immediate rather
        # than up to self.interval late.
        while not self._stop.wait(self.interval):
            self.poll()

    def stop(self):
        """Stop the watcher, then report once more, so whatever appeared during the last
        interval isn't lost behind the closing summary. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1.0)
            self._thread = None
        self.poll()


def check_expectations(expectations, name, log=_emit, skip=0):
    """Settle-wait, evaluate the ledger's rows after the first `skip`, print the report.
    Returns an exit code."""
    settled, rows = ngt_expectations.wait_for_settle(expectations, log=log, skip=skip)
    verdict = ngt_expectations.evaluate(expectations, rows)
    verdict.settled = settled
    if not settled:
        verdict.ok = False
        verdict.problems.append(
            f"uploads were still arriving after settle_timeout={expectations.settle_timeout:.0f}s -- "
            "inconclusive: the pipeline had not finished"
        )
    log(ngt_expectations.format_report(verdict, expectations, name=name))
    return EXIT_OK if verdict.ok else EXIT_EXPECTATIONS_FAILED


def main():
    args = parse_args()

    speed = 1.0 if args.speed is None else args.speed
    scenario = _load_scenario(args.scenario)

    # A malformed scenario exits 2, without a traceback; expectations that didn't hold exit 1.
    try:
        events = build_timeline(scenario)
        raw_expect = scenario.get("expect") if isinstance(scenario, dict) else None
        expectations = (
            None if raw_expect is None or args.no_validate else ngt_expectations.parse_expectations(raw_expect)
        )
    except (ValueError, ngt_faults.FaultSpecError) as exc:
        print(f"{args.scenario}: {exc}", file=sys.stderr)
        return EXIT_BAD_SCENARIO

    if args.only_list_calibrations:
        # "only" is the contract: every other flag is ignored rather than rejected, so
        # whatever drives this script can append this one to a command line it already
        # has instead of picking the scenario path out of it. One name per line and
        # nothing else on stdout -- an empty scenario prints nothing at all, rather than
        # prose a caller looping over the output would take for a calibration name.
        # The scenario was parsed above, so a malformed one fails here, before whoever
        # asked has started anything. Fault events are skipped: an oms fault has no
        # calibration at all, and a scoped one never needs loops its runs don't feed.
        for calibration in sorted({e.calibration for e in events if e.kind != "fault"}):
            _emit(calibration)
        return None

    if args.validate_only:
        if expectations is None:
            print(f"{args.scenario}: --validate-only needs an expect: block", file=sys.stderr)
            return EXIT_BAD_SCENARIO
        return check_expectations(expectations, name=str(args.scenario))

    if not events:
        _emit("(scenario has no runs -- nothing to play)")
        return None

    if args.dry_run:
        show_timeline(events)
        _emit(expectations.describe() if expectations else "expect: (none -- scenario is not self-validating)")
        return None

    # Only this play's uploads are checked: an earlier play's stay in the ledger until a reset.
    uploads_before = len(fake_conddb.read_ledger())
    # Constructing the monitor is what baselines the data directory, so it has to happen
    # before the first event fires.
    monitor = DataDirMonitor(seed.NGT_DEV_HOME / "data") if args.monitor else None
    how = "waiting for confirmation before each step" if args.interactive else f"at {speed}x speed"
    _emit(f"Playing {args.scenario} {how} ({len(events)} events, $NGT_DEV_HOME={seed.NGT_DEV_HOME})")
    if monitor is not None:
        missing = "" if monitor.root.is_dir() else " (it doesn't exist yet -- has live_demo.sh setup run?)"
        _emit(f"Watching {monitor.root} every {monitor.interval:g}s for whatever the loops produce{missing}")
        monitor.start()

    try:
        play(events, speed=speed, prompt=input if args.interactive else None)
    except KeyboardInterrupt:
        _emit("\nInterrupted -- the remaining events were not fired.")
        status = 130
    except EOFError:
        _emit(
            "\nNothing left on stdin to confirm with -- stopping. (--interactive wants a terminal, or "
            "something feeding it; plain or --speed playback is the unattended mode.)"
        )
        status = 2
    else:
        status = None

    if status is not None:
        # Take the monitor's last report, but skip the wait below: having to escape a
        # second prompt after a Ctrl-C would just be rude.
        if monitor is not None:
            monitor.stop()
        return status

    if monitor is None:
        _emit("Scenario finished.")
    else:
        _emit(f"Scenario finished ({len(events)} events) -- Processing workflows may still be working.")

    # The settle wait runs while --monitor's watcher thread keeps reporting, so the files
    # behind each upload still show up as they land.
    result = None
    if expectations is not None:
        result = check_expectations(expectations, name=str(args.scenario), skip=uploads_before)

    if monitor is not None:
        # keep monitoring until a user's signal
        try:
            input(">>> Still watching. Press Enter to stop and exit (Ctrl-C works too)... ")
        except KeyboardInterrupt:
            _emit("")  # step past the echoed ^C
        except EOFError:
            _emit("(stdin closed -- not waiting.)")
        monitor.stop()
        _emit(f"Stopped watching {monitor.root}.")
    return result


if __name__ == "__main__":
    sys.exit(main())
