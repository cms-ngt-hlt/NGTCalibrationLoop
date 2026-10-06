"""Tests for scenario-player/faults.py: FaultSpec validation and the fault-file helpers."""
import json

import pytest

import faults

OMS = {"target": "oms", "mode": "timeout"}
CMSRUN = {"target": "cmsrun", "mode": "exit_code"}


def test_defaults():
    assert faults.parse_fault_spec(OMS) == faults.FaultSpec(target="oms", mode="timeout", times=1)
    assert faults.parse_fault_spec(CMSRUN).exit_code == 1


@pytest.mark.parametrize(
    "raw, field, value",
    [
        ({"target": "oms", "mode": "http_error", "status": 503}, "status", 503),
        (
            {**CMSRUN, "exit_code": 139, "calibration": "EcalPedestals", "run": 398600, "step": "step2"},
            "step",
            "step2",
        ),
        ({**OMS, "times": "unlimited"}, "times", None),
    ],
)
def test_accepts(raw, field, value):
    assert getattr(faults.parse_fault_spec(raw), field) == value


@pytest.mark.parametrize(
    "raw, match",
    [
        *[({**OMS, "times": t}, "times") for t in (0, -1, 1.5, "soon", True)],
        ({"target": "eos", "mode": "exit_code"}, "target"),
        ({"target": "oms", "mode": "exit_code"}, "mode"),
        ({"target": "cmsrun", "mode": "timeout"}, "mode"),
        *[({"target": "oms", "mode": "http_error", "status": s}, "status") for s in (None, 399, "503")],
        ({**CMSRUN, "exit_code": 0}, "exit_code"),
        ({**CMSRUN, "step": "step5"}, "step"),
        # fields the target's consumer never looks at, so the fault could not fire as written
        ({**OMS, "calibration": "EcalPedestals"}, "takes only"),
        ({"target": "upload_conditions", "mode": "exit_code", "step": "step4"}, "takes only"),
        ({**OMS, "bogus": 1}, "takes only"),
    ],
)
def test_rejects(raw, match):
    with pytest.raises(faults.FaultSpecError, match=match):
        faults.parse_fault_spec(raw)


def test_describe_names_the_target_and_only_the_fields_that_are_set():
    entry = {"mode": "exit_code", "times_remaining": 1, "exit_code": 139, "calibration": "EcalPedestals", "step": "step2"}
    assert faults.describe("cmsrun", entry) == (
        "(FAKE) injected cmsrun fault: mode=exit_code exit_code=139 calibration=EcalPedestals step=step2"
    )


# --- the fault files ---------------------------------------------------------------------


def test_fault_spec_to_entry_omits_target_and_unset_fields():
    spec = faults.parse_fault_spec({**OMS, "run": 398600})
    assert faults.fault_spec_to_entry(spec) == {"mode": "timeout", "times_remaining": 1, "run": 398600}


def test_scope_matches():
    scope = {"calibration": "EcalPedestals", "run": 398600, "step": "step2"}
    assert faults.scope_matches({"mode": "exit_code"}, scope)  # unscoped
    assert faults.scope_matches({"calibration": "EcalPedestals", "run": 398600}, scope)
    assert not faults.scope_matches({"calibration": "SiStripBad"}, scope)
    assert not faults.scope_matches({"run": 500201}, scope)
    assert faults.scope_matches({"run": 398600}, {**scope, "run": "398600"})  # compared as strings


def _armed(tmp_path, *entries):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(path, list(entries))
    return path


def _left(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_consume_fault_missing_file_returns_none(tmp_path):
    assert faults.consume_fault(tmp_path / "nope.json", {}) is None


def test_consume_fault_counts_down_and_removes_a_spent_entry(tmp_path):
    path = _armed(tmp_path, {"mode": "exit_code", "exit_code": 1, "times_remaining": 2})

    assert faults.consume_fault(path, {})["exit_code"] == 1
    assert _left(path) == [{"mode": "exit_code", "exit_code": 1, "times_remaining": 1}]
    faults.consume_fault(path, {})
    assert _left(path) == []


def test_consume_fault_unlimited_never_removes_the_entry(tmp_path):
    path = _armed(tmp_path, {"mode": "exit_code", "exit_code": 1, "times_remaining": None})

    for _ in range(5):
        assert faults.consume_fault(path, {})["exit_code"] == 1
    assert len(_left(path)) == 1


def test_consume_fault_takes_the_first_matching_entry(tmp_path):
    path = _armed(
        tmp_path,
        {"mode": "exit_code", "exit_code": 1, "calibration": "SiStripBad", "times_remaining": 1},
        {"mode": "exit_code", "exit_code": 2, "calibration": "EcalPedestals", "times_remaining": 1},
    )

    assert faults.consume_fault(path, {"calibration": "EcalPedestals", "run": 1, "step": "step2"})["exit_code"] == 2
    assert [e["exit_code"] for e in _left(path)] == [1]
