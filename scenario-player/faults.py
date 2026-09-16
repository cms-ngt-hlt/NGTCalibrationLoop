"""Shared vocabulary for injecting failures into OMS / cmsRun /
upload_conditions.py during testing.

A "fault" is a small, declarative description of one failure to simulate --
which external system (`target`), how it fails (`mode`), an optional scope
(`calibration`/`run`/`step`), how many matching calls it survives (`times`),
and an optional realistic-looking log/error `message` so a failure looks
authentic to someone debugging it (e.g. via the Airflow UI's task logs)
rather than a bare "simulated failure" placeholder.

The same FaultSpec shape is used by two different backends, since pytest and
the live scenario-player/scenario_player.py workflow run in different process
topologies:

- pytest (tests/conftest.py, tests/stubs/omsapi) applies a FaultSpec directly,
  in-process, via monkeypatched fixtures.
- the live player (scenario-player/seed.py, scenario-player/scenario_player.py) arms a FaultSpec by
  writing it to a JSON file under $NGT_DEV_HOME/faults/<target>.json, which
  scenario-player/bin/cmsRun, scenario-player/bin/uploadConditions.py, and tests/stubs/omsapi (in its
  live-demo mode) poll and consume across the process boundary to Airflow's
  own scheduler/triggerer/worker processes.

This module only defines/validates the vocabulary and the file-locking
helpers the file-based backend needs -- it has no Airflow, OMS, or CMSSW
dependency of its own.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

TARGETS = ("oms", "cmsrun", "upload_conditions")
OMS_MODES = ("connection_error", "timeout", "http_error", "malformed_json")
JOB_MODES = ("exit_code",)
STEPS = ("step2", "step3", "step4")


class FaultSpecError(ValueError):
    """Raised for an invalid fault entry, e.g. from a scenario YAML's `faults:` list."""


@dataclass(frozen=True)
class FaultSpec:
    target: str
    mode: str
    times: Optional[int] = 1  # None = unlimited
    status: Optional[int] = None
    exit_code: Optional[int] = None
    calibration: Optional[str] = None
    run: Optional[int] = None
    step: Optional[str] = None
    message: Optional[str] = None


def _require(condition, message):
    if not condition:
        raise FaultSpecError(message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_fault_spec(raw: dict) -> FaultSpec:
    """Validate one `faults:` entry (a plain dict, e.g. parsed from YAML) into
    a FaultSpec. Raises FaultSpecError, naming the offending field, on
    anything invalid.

    `at` (scenario-player/scenario_player.py's playback-timeline offset) is deliberately
    not a FaultSpec field -- it's only meaningful to that module's Event, not
    to the fault itself -- so callers must pop it before calling this.
    """
    raw = dict(raw)

    target = raw.pop("target", None)
    _require(target in TARGETS, f"faults: 'target' must be one of {TARGETS}, got {target!r}")

    mode = raw.pop("mode", None)
    valid_modes = OMS_MODES if target == "oms" else JOB_MODES
    _require(
        mode in valid_modes,
        f"faults: target={target!r} 'mode' must be one of {valid_modes}, got {mode!r}",
    )

    # times: 1 when omitted (single-shot, matching every pre-existing
    # single-shot failure hook this replaces); an explicit None or the YAML/
    # CLI spelling "unlimited" both mean unlimited -- accepting bare None
    # here (not just the string) lets a FaultSpec that already round-tripped
    # once (e.g. scenario-player/seed.py:arm_fault re-validating a spec built from an
    # already-parsed Event) pass its own .times straight back through.
    times_raw = raw.pop("times", 1)
    if times_raw is None or times_raw == "unlimited":
        times = None
    else:
        _require(
            _is_int(times_raw) and times_raw > 0,
            f"faults: 'times' must be a positive integer or 'unlimited', got {times_raw!r}",
        )
        times = times_raw

    status = raw.pop("status", None)
    if target == "oms" and mode == "http_error":
        _require(
            _is_int(status) and 400 <= status <= 599,
            "faults: target=oms mode=http_error requires an integer 'status' between 400 and 599",
        )
    else:
        _require(status is None, "faults: 'status' is only valid for target=oms mode=http_error")

    exit_code = raw.pop("exit_code", None)
    if target in ("cmsrun", "upload_conditions"):
        _require(
            _is_int(exit_code) and exit_code != 0,
            f"faults: target={target!r} requires a nonzero integer 'exit_code'",
        )
    else:
        _require(exit_code is None, "faults: 'exit_code' is only valid for target=cmsrun/upload_conditions")

    calibration = raw.pop("calibration", None)
    if target in ("cmsrun", "upload_conditions"):
        _require(
            isinstance(calibration, str) and calibration,
            f"faults: target={target!r} requires a 'calibration' (e.g. 'EcalPedestals') -- faults on "
            "this target must always be scoped so one can't fire against the wrong calibration's job "
            "in a multi-calibration scenario",
        )
    else:
        _require(calibration is None, "faults: 'calibration' is not valid for target=oms")

    run = raw.pop("run", None)
    _require(run is None or _is_int(run), f"faults: 'run' must be an integer if given, got {run!r}")

    step = raw.pop("step", None)
    if step is not None:
        _require(target == "cmsrun", "faults: 'step' is only valid for target=cmsrun")
        _require(step in STEPS, f"faults: 'step' must be one of {STEPS}, got {step!r}")

    message = raw.pop("message", None)
    _require(
        message is None or isinstance(message, str),
        f"faults: 'message' must be a string if given, got {message!r}",
    )

    _require(not raw, f"faults: unknown field(s) {sorted(raw)} in a target={target!r} fault entry")

    return FaultSpec(
        target=target,
        mode=mode,
        times=times,
        status=status,
        exit_code=exit_code,
        calibration=calibration,
        run=run,
        step=step,
        message=message,
    )


def parse_fault_specs(raw_list):
    """Validate a whole `faults:` list, preserving order."""
    return [parse_fault_spec(entry) for entry in raw_list]


def fault_spec_to_entry(spec: FaultSpec) -> dict:
    """FaultSpec -> a plain JSON-serializable dict for the fault-file backend
    (scenario-player/seed.py's arm_fault), with a fresh times_remaining counter. `target`
    is omitted -- the caller already knows it, since it picks which
    <target>.json file to write."""
    entry = {"mode": spec.mode, "times_remaining": spec.times}
    for field in ("status", "exit_code", "calibration", "run", "step", "message"):
        value = getattr(spec, field)
        if value is not None:
            entry[field] = value
    return entry


def atomic_write_json(path, data):
    """Write `data` as JSON to `path` atomically (temp file + os.replace), so
    a concurrent reader never observes a partially-written file."""
    path = str(path)
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-fault-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def locked_json_file(path):
    """Hold an exclusive, cross-process advisory lock for the duration of the
    `with` block, so a read-modify-write cycle (arming or consuming a fault)
    is never interleaved with another process's.

    POSIX-only (fcntl.flock), imported lazily so this module still imports
    cleanly on Windows -- only the live-test setup this guards actually calls
    it, and that already requires a Linux/WSL host (see README.md), so this
    is not a portability regression.
    """
    import fcntl

    lock_path = str(path) + ".lock"
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def scope_matches(entry: dict, scope: dict) -> bool:
    """True if an armed fault-file entry applies to this invocation's scope.
    A key absent from `entry` (unscoped on that axis) always matches; a key
    present must match (as strings, so an int run number compares equal to
    itself regardless of either side's type)."""
    for key in ("calibration", "run", "step"):
        if key in entry and str(entry[key]) != str(scope.get(key)):
            return False
    return True


def consume_fault(path, scope):
    """Read the fault file at `path` (a list of armed entries), find the
    first one matching `scope`, decrement its times_remaining (removing it if
    exhausted), write the file back, and return the matched entry (before
    decrementing) -- or None if the file is missing/empty or nothing matches.
    Caller is expected to hold locked_json_file(path) around this.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    for i, entry in enumerate(entries):
        if scope_matches(entry, scope):
            matched = dict(entry)
            remaining = entry.get("times_remaining")
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    entries.pop(i)
                else:
                    entry["times_remaining"] = remaining
            atomic_write_json(path, entries)
            return matched
    return None
