"""Shared vocabulary for injecting failures into OMS / cmsRun / upload_conditions.py.

A fault names the system (`target`), how it fails (`mode`), an optional scope
(`calibration`/`run`/`step`) and how many matching calls it survives (`times`).
Whatever consumes it prints describe()'s one-line text.

pytest arms the fake OMS in-process with tests/stubs/omsapi's set_failure(); a
live scenario arms faults by writing $NGT_DEV_HOME/faults/<target>.json
(seed.py's arm_fault), which the fakes consume in the processes running the
loops. Standard library only, so the fakes in bin/ can import it.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

TARGETS = ("oms", "cmsrun", "upload_conditions")
MODES = {
    "oms": ("connection_error", "timeout", "http_error", "malformed_json"),
    "cmsrun": ("exit_code",),
    "upload_conditions": ("exit_code",),
}
STEPS = ("step2", "step3", "step4")
# What each target's consumer looks at: a fault carrying any other field could never fire as written
# (an OMS query has no calibration in its scope, an upload has no step).
FIELDS = {
    "oms": {"mode", "times", "run", "status"},
    "cmsrun": {"mode", "times", "run", "calibration", "step", "exit_code"},
    "upload_conditions": {"mode", "times", "run", "calibration", "exit_code"},
}


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


def _require(condition, message):
    if not condition:
        raise FaultSpecError(message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_fault_spec(raw: dict) -> FaultSpec:
    """Validate one `faults:` entry into a FaultSpec, raising FaultSpecError naming the
    offending field. A field set to None counts as unset; a scenario's `at` is the caller's to pop."""
    fields = {key: value for key, value in raw.items() if value is not None}
    _validate_fields(fields)

    if fields["target"] != "oms":
        fields.setdefault("exit_code", 1)
    if fields.get("times") == "unlimited":
        fields["times"] = None
    return FaultSpec(**fields)


def _validate_fields(fields: dict):
    """Raise FaultSpecError unless `fields` -- a `faults:` entry without its unset fields --
    is a fault its target's consumer acts on."""
    target = fields.get("target")
    _require(target in TARGETS,
             f"faults: 'target' must be one of {TARGETS}, got {target!r}")

    allowed = FIELDS[target]
    unknown = set(fields) - {"target"} - allowed
    _require(not unknown,
             f"faults: target={target!r} takes only {sorted(allowed)}, got {sorted(unknown)}")

    mode = fields.get("mode")
    modes = MODES[target]
    _require(mode in modes,
             f"faults: target={target!r} 'mode' must be one of {modes}, got {mode!r}")

    times = fields.get("times", 1)
    _require(times == "unlimited" or (_is_int(times) and times > 0),
             f"faults: 'times' must be a positive integer or 'unlimited', got {times!r}")

    if mode == "http_error":
        status = fields.get("status")
        _require(_is_int(status) and status >= 400,  # below 400, raise_for_status() never raises
                 f"faults: http_error needs an int 'status' >= 400, got {status!r}")

    if "exit_code" in fields:
        exit_code = fields["exit_code"]
        _require(_is_int(exit_code) and exit_code != 0,
                 f"faults: 'exit_code' must be a nonzero int, got {exit_code!r}")

    if "step" in fields:
        step = fields["step"]
        _require(step in STEPS,
                 f"faults: 'step' must be one of {STEPS}, got {step!r}")


def fault_spec_to_entry(spec: FaultSpec) -> dict:
    """FaultSpec -> a fault-file entry with a fresh times_remaining counter
    (no `target`: that picks the file)."""
    entry = {"mode": spec.mode, "times_remaining": spec.times}
    for field in ("status", "exit_code", "calibration", "run", "step"):
        value = getattr(spec, field)
        if value is not None:
            entry[field] = value
    return entry


def describe(target, fault):
    """The error text an injected fault produces, e.g.
    "(FAKE) injected cmsrun fault: mode=exit_code exit_code=139 calibration=EcalPedestals step=step2"."""
    keys = ("mode", "status", "exit_code", "calibration", "run", "step")
    details = " ".join(f"{key}={fault[key]}" for key in keys if fault.get(key) is not None)
    return f"(FAKE) injected {target} fault: {details}"


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
    """Exclusive cross-process lock around a read-modify-write of `path`.
    fcntl is imported lazily, so the module still imports on Windows."""
    import fcntl

    lock_path = str(path) + ".lock"
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def scope_matches(entry: dict, scope: dict) -> bool:
    """True if every scope key the entry sets matches `scope` (compared as strings)."""
    for key in ("calibration", "run", "step"):
        if key in entry and str(entry[key]) != str(scope.get(key)):
            return False
    return True


def take_fault(entries, scope):
    """Count down the first entry matching `scope` (removing it once spent) and
    return it as it was, or None. Edits `entries` in place."""
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
            return matched
    return None


def consume_fault(path, scope):
    """take_fault() on the fault file at `path`, written back if anything
    matched. The caller holds locked_json_file(path)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    matched = take_fault(entries, scope)
    if matched is not None:
        atomic_write_json(path, entries)
    return matched
