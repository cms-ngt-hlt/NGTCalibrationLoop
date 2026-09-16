"""Tests for scenario-player/faults.py: FaultSpec validation and the
file-based arm/consume helpers the live scenario-player backend uses.
"""
import json

import pytest

import faults


def test_parses_minimal_oms_fault():
    spec = faults.parse_fault_spec({"target": "oms", "mode": "connection_error"})
    assert spec.target == "oms"
    assert spec.mode == "connection_error"
    assert spec.times == 1  # default: single-shot
    assert spec.calibration is None
    assert spec.run is None


def test_parses_oms_http_error_with_status():
    spec = faults.parse_fault_spec({"target": "oms", "mode": "http_error", "status": 503})
    assert spec.status == 503


def test_parses_cmsrun_fault_with_full_scope():
    spec = faults.parse_fault_spec(
        {
            "target": "cmsrun",
            "mode": "exit_code",
            "exit_code": 139,
            "calibration": "EcalPedestals",
            "run": 398600,
            "step": "step2",
            "times": 2,
        }
    )
    assert spec.exit_code == 139
    assert spec.calibration == "EcalPedestals"
    assert spec.run == 398600
    assert spec.step == "step2"
    assert spec.times == 2


def test_parses_upload_conditions_fault():
    spec = faults.parse_fault_spec(
        {"target": "upload_conditions", "mode": "exit_code", "exit_code": 1, "calibration": "SiStripBad"}
    )
    assert spec.target == "upload_conditions"
    assert spec.calibration == "SiStripBad"


def test_times_unlimited_string_becomes_none():
    spec = faults.parse_fault_spec({"target": "oms", "mode": "timeout", "times": "unlimited"})
    assert spec.times is None


@pytest.mark.parametrize("bad_times", [0, -1, 1.5, "soon", True])
def test_rejects_bad_times(bad_times):
    with pytest.raises(faults.FaultSpecError, match="times"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "times": bad_times})


def test_rejects_unknown_target():
    with pytest.raises(faults.FaultSpecError, match="target"):
        faults.parse_fault_spec({"target": "eos", "mode": "exit_code"})


def test_rejects_unknown_mode_for_target():
    with pytest.raises(faults.FaultSpecError, match="mode"):
        faults.parse_fault_spec({"target": "oms", "mode": "exit_code"})
    with pytest.raises(faults.FaultSpecError, match="mode"):
        faults.parse_fault_spec({"target": "cmsrun", "mode": "timeout", "calibration": "EcalPedestals"})


def test_http_error_requires_status():
    with pytest.raises(faults.FaultSpecError, match="status"):
        faults.parse_fault_spec({"target": "oms", "mode": "http_error"})


@pytest.mark.parametrize("bad_status", [399, 600, "503", 503.0])
def test_http_error_rejects_out_of_range_or_wrong_type_status(bad_status):
    with pytest.raises(faults.FaultSpecError, match="status"):
        faults.parse_fault_spec({"target": "oms", "mode": "http_error", "status": bad_status})


def test_status_rejected_outside_oms_http_error():
    with pytest.raises(faults.FaultSpecError, match="status"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "status": 503})


def test_cmsrun_requires_nonzero_exit_code():
    with pytest.raises(faults.FaultSpecError, match="exit_code"):
        faults.parse_fault_spec({"target": "cmsrun", "mode": "exit_code", "calibration": "EcalPedestals"})
    with pytest.raises(faults.FaultSpecError, match="exit_code"):
        faults.parse_fault_spec(
            {"target": "cmsrun", "mode": "exit_code", "exit_code": 0, "calibration": "EcalPedestals"}
        )


def test_exit_code_rejected_for_oms():
    with pytest.raises(faults.FaultSpecError, match="exit_code"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "exit_code": 1})


@pytest.mark.parametrize("target", ["cmsrun", "upload_conditions"])
def test_calibration_required_for_job_targets(target):
    raw = {"target": target, "mode": "exit_code", "exit_code": 1}
    with pytest.raises(faults.FaultSpecError, match="calibration"):
        faults.parse_fault_spec(raw)


def test_calibration_rejected_for_oms():
    with pytest.raises(faults.FaultSpecError, match="calibration"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "calibration": "EcalPedestals"})


def test_step_only_valid_for_cmsrun():
    with pytest.raises(faults.FaultSpecError, match="step"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "step": "step2"})
    with pytest.raises(faults.FaultSpecError, match="step"):
        faults.parse_fault_spec(
            {
                "target": "upload_conditions",
                "mode": "exit_code",
                "exit_code": 1,
                "calibration": "EcalPedestals",
                "step": "step4",
            }
        )


def test_step_must_be_a_known_step():
    with pytest.raises(faults.FaultSpecError, match="step"):
        faults.parse_fault_spec(
            {
                "target": "cmsrun",
                "mode": "exit_code",
                "exit_code": 1,
                "calibration": "EcalPedestals",
                "step": "step5",
            }
        )


def test_run_must_be_an_integer():
    with pytest.raises(faults.FaultSpecError, match="run"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "run": "398600"})


def test_message_must_be_a_string():
    with pytest.raises(faults.FaultSpecError, match="message"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "message": 123})


def test_message_is_passed_through():
    spec = faults.parse_fault_spec({"target": "oms", "mode": "timeout", "message": "custom failure text"})
    assert spec.message == "custom failure text"


def test_rejects_unknown_field():
    with pytest.raises(faults.FaultSpecError, match="unknown"):
        faults.parse_fault_spec({"target": "oms", "mode": "timeout", "bogus": 1})


def test_parse_fault_specs_preserves_order():
    raw_list = [
        {"target": "oms", "mode": "timeout"},
        {"target": "cmsrun", "mode": "exit_code", "exit_code": 1, "calibration": "EcalPedestals"},
        {"target": "upload_conditions", "mode": "exit_code", "exit_code": 1, "calibration": "BeamSpot"},
    ]
    specs = faults.parse_fault_specs(raw_list)
    assert [s.target for s in specs] == ["oms", "cmsrun", "upload_conditions"]


# --- fault_spec_to_entry / atomic_write_json / locked_json_file / consume_fault ---


def test_fault_spec_to_entry_omits_target_and_none_fields():
    spec = faults.parse_fault_spec({"target": "oms", "mode": "timeout", "run": 398600})
    entry = faults.fault_spec_to_entry(spec)
    assert "target" not in entry
    assert entry == {"mode": "timeout", "times_remaining": 1, "run": 398600}


def test_atomic_write_json_round_trips(tmp_path):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(path, [{"a": 1}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"a": 1}]
    # no leftover temp files
    assert list(tmp_path.iterdir()) == [path]


def test_scope_matches_unscoped_entry_matches_anything():
    assert faults.scope_matches({"mode": "exit_code"}, {"calibration": "EcalPedestals", "run": 398600, "step": "step2"})


def test_scope_matches_requires_all_present_keys_to_match():
    entry = {"calibration": "EcalPedestals", "run": 398600}
    assert faults.scope_matches(entry, {"calibration": "EcalPedestals", "run": 398600, "step": "step2"})
    assert not faults.scope_matches(entry, {"calibration": "SiStripBad", "run": 398600, "step": "step2"})
    assert not faults.scope_matches(entry, {"calibration": "EcalPedestals", "run": 500201, "step": "step2"})


def test_scope_matches_compares_run_as_string_regardless_of_type():
    entry = {"run": 398600}
    assert faults.scope_matches(entry, {"calibration": "x", "run": "398600", "step": "step2"})


def test_consume_fault_missing_file_returns_none(tmp_path):
    assert faults.consume_fault(tmp_path / "nope.json", {}) is None


def test_consume_fault_single_shot_removes_entry(tmp_path):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(path, [{"mode": "exit_code", "exit_code": 1, "times_remaining": 1}])

    matched = faults.consume_fault(path, {"calibration": "EcalPedestals", "run": 1, "step": "step2"})
    assert matched["exit_code"] == 1
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_consume_fault_decrements_without_removing_when_times_remain(tmp_path):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(path, [{"mode": "exit_code", "exit_code": 1, "times_remaining": 2}])

    faults.consume_fault(path, {})
    remaining = json.loads(path.read_text(encoding="utf-8"))
    assert remaining == [{"mode": "exit_code", "exit_code": 1, "times_remaining": 1}]


def test_consume_fault_unlimited_never_removes_entry(tmp_path):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(path, [{"mode": "exit_code", "exit_code": 1, "times_remaining": None}])

    for _ in range(5):
        matched = faults.consume_fault(path, {})
        assert matched["exit_code"] == 1
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


def test_consume_fault_skips_non_matching_entries_in_order(tmp_path):
    path = tmp_path / "cmsrun.json"
    faults.atomic_write_json(
        path,
        [
            {"mode": "exit_code", "exit_code": 1, "calibration": "SiStripBad", "times_remaining": 1},
            {"mode": "exit_code", "exit_code": 2, "calibration": "EcalPedestals", "times_remaining": 1},
        ],
    )
    matched = faults.consume_fault(path, {"calibration": "EcalPedestals", "run": 1, "step": "step2"})
    assert matched["exit_code"] == 2
    remaining = json.loads(path.read_text(encoding="utf-8"))
    assert len(remaining) == 1 and remaining[0]["exit_code"] == 1
