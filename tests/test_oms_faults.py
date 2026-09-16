"""Tests for the richer tests/stubs/omsapi set_failure() modes: which of
oms.py's four functions propagate a given fault type vs. swallow it into
their documented safe fallback, times/run scoping, backward compatibility
with the original bool form, and the file-based (live-player) fault path.

oms.py's asymmetry (only find_new_run's raise_for_status()/json() path is
defensively wrapped, and only against JSONDecodeError specifically) is
already documented by test_step2_lib.py's test_new_run_available_oms_outage_
propagates; these tests extend that documentation across all 4 fault modes
and all 4 oms.py functions using real requests exception types instead of a
generic RuntimeError.
"""
import json
from datetime import datetime, timedelta, timezone

import omsapi
import pytest
import requests

import faults as ngt_faults
from ngt_calibration_loop import oms, step2

CALIBRATION = "EcalPedestals"


def make_run(run_number, start_time, end_time=None, last_ls=100):
    return {
        "run_number": run_number,
        "fill_type_runtime": "PROTONS",
        "l1_hlt_mode": "collisions2026",
        "stable_beam": True,
        "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ") if end_time else None,
        "last_lumisection_number": last_ls,
    }


# --- find_new_run: the one function with a narrow, mode-specific except clause ------


@pytest.mark.parametrize(
    "mode,exc_cls",
    [("connection_error", requests.exceptions.ConnectionError), ("timeout", requests.exceptions.Timeout)],
)
def test_find_new_run_propagates_connection_and_timeout_errors(isolated_env, mode, exc_cls):
    """find_new_run's try/except only wraps raise_for_status()/json(), not the
    q.data() call itself -- connection_error/timeout raise from .data() and
    so are never caught at all."""
    omsapi.set_failure(mode)
    with pytest.raises(exc_cls):
        step2.find_new_run(CALIBRATION)


def test_find_new_run_raises_http_error(isolated_env):
    """find_new_run does call raise_for_status(), so an http_error-mode fault
    (unlike connection_error/timeout) surfaces as a real requests.HTTPError."""
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=None)])
    omsapi.set_failure("http_error", status=503)

    with pytest.raises(requests.exceptions.HTTPError):
        step2.find_new_run(CALIBRATION)


def test_find_new_run_returns_none_on_malformed_json(isolated_env):
    """The one fault mode find_new_run actually catches (JSONDecodeError) --
    it returns None instead of propagating, matching every other "OMS had a
    problem" outcome in this function."""
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=None)])
    omsapi.set_failure("malformed_json")

    assert step2.find_new_run(CALIBRATION) is None


def test_run_scoped_fault_never_matches_find_new_run(isolated_env):
    """find_new_run's own query never filters on run_number (it's searching
    FOR a run, not checking a known one) -- a run-scoped fault can only ever
    match daq_is_running/last_ls_run_number/run_end_time, never this."""
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=None)])
    omsapi.set_failure("connection_error", run=398600)

    assert step2.find_new_run(CALIBRATION) == 398600


def test_bool_true_fails_every_call_with_no_countdown(isolated_env):
    """Backward-compat lock-in: the original set_failure(True)/(False) form
    (used by test_step2_lib.py and others) must keep behaving as an
    unconditional, persistent toggle, not a single-shot fault."""
    omsapi.set_failure(True)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            step2.find_new_run(CALIBRATION)


# --- daq_is_running / last_ls_run_number / run_end_time: broad except Exception -----


@pytest.mark.parametrize("mode", ["connection_error", "timeout", "malformed_json"])
def test_daq_is_running_swallows_and_falls_back(mode):
    omsapi.set_failure(mode)
    assert oms.daq_is_running(398600) == (False, None)


@pytest.mark.parametrize("mode", ["connection_error", "timeout", "malformed_json"])
def test_last_ls_run_number_swallows_and_falls_back(mode):
    omsapi.set_failure(mode)
    assert oms.last_ls_run_number(398600) == 0


@pytest.mark.parametrize("mode", ["connection_error", "timeout", "malformed_json"])
def test_run_end_time_swallows_and_falls_back(mode):
    omsapi.set_failure(mode)
    assert oms.run_end_time(398600) is None


def test_http_error_does_not_affect_daq_is_running():
    """Documents the status-code-blindness gap: daq_is_running only calls
    .data().json(), never .raise_for_status() -- an http_error-mode fault
    (which only manifests via raise_for_status()) has no effect here at all,
    so this asserts the call succeeds normally despite the armed fault."""
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=None)])
    omsapi.set_failure("http_error", status=503, run=398600)

    is_running, _last_ls = oms.daq_is_running(398600)
    assert is_running is True


def test_http_error_does_not_affect_last_ls_run_number():
    omsapi.configure_runs([make_run(398600, start_time=datetime.now(timezone.utc), last_ls=42)])
    omsapi.set_failure("http_error", status=500, run=398600)

    assert oms.last_ls_run_number(398600) == 42


# --- times / run scoping -------------------------------------------------------------


def test_times_self_clears_after_n_calls():
    now = datetime.now(timezone.utc)
    omsapi.configure_runs([make_run(398600, start_time=now - timedelta(hours=1), end_time=None)])
    omsapi.set_failure("connection_error", times=2)

    assert oms.daq_is_running(398600) == (False, None)  # 1st: fault fires
    assert oms.daq_is_running(398600) == (False, None)  # 2nd: fault fires
    # 3rd: fault exhausted -- is_running=True can only come from the real,
    # non-exception path (the swallow fallback is always False).
    is_running, _last_ls = oms.daq_is_running(398600)
    assert is_running is True


def test_unlimited_times_never_clears():
    omsapi.set_failure("connection_error", times=None)
    for _ in range(4):
        assert oms.daq_is_running(398600) == (False, None)


def test_run_scoping_only_affects_the_matching_run_number():
    now = datetime.now(timezone.utc)
    omsapi.configure_runs(
        [
            make_run(398600, start_time=now - timedelta(hours=1), end_time=None),
            make_run(398601, start_time=now - timedelta(hours=1), end_time=None),
        ]
    )
    omsapi.set_failure("connection_error", run=398600)

    is_running, _ = oms.daq_is_running(398601)  # different run: unaffected
    assert is_running is True

    is_running, _ = oms.daq_is_running(398600)  # scoped run: fault fires
    assert is_running is False


# --- message override / realistic default text ---------------------------------------


def test_custom_message_used_verbatim(isolated_env):
    """Uses find_new_run (the one propagating function) rather than
    daq_is_running -- daq_is_running swallows the exception by design (see
    test_daq_is_running_swallows_and_falls_back above), so it can't be used
    to observe the raised exception's own message."""
    omsapi.set_failure("connection_error", message="custom OMS-is-down message")
    with pytest.raises(requests.exceptions.ConnectionError, match="custom OMS-is-down message"):
        step2.find_new_run(CALIBRATION)


def test_default_message_looks_like_a_real_oms_outage(isolated_env):
    omsapi.set_failure("connection_error")
    with pytest.raises(requests.exceptions.ConnectionError, match="cmsoms.cms"):
        step2.find_new_run(CALIBRATION)


# --- file-based fault path (the live scenario-player's cross-process route) ----------


def test_file_based_fault_is_consumed_like_the_in_memory_one(monkeypatch, tmp_path):
    """scenario-player/scenario_player.py runs as a separate OS process from Airflow, so
    it can't call set_failure() directly -- it writes to
    NGT_OMS_STUB_FAULTS_FILE instead (via scenario-player/seed.py's arm_fault()), which
    this stub polls exactly like NGT_OMS_STUB_RUNS_FILE. This exercises that
    path directly, without needing a second process."""
    faults_file = tmp_path / "oms.json"
    ngt_faults.atomic_write_json(
        faults_file, [{"mode": "connection_error", "times_remaining": 1, "run": 398600}]
    )
    monkeypatch.setenv("NGT_OMS_STUB_FAULTS_FILE", str(faults_file))

    is_running, _ = oms.daq_is_running(398600)
    assert is_running is False

    assert json.loads(faults_file.read_text(encoding="utf-8")) == []  # single-shot: consumed and removed
