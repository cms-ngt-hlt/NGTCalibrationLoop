"""Tests for tests/stubs/omsapi's faults: which real `requests` exception each mode raises and from
which call, that a fault is single-shot by default, `run` scoping and the live fault file. Counting
down is faults.take_fault's, tested in test_faults.py; how a caller reacts to a fault is for that
caller's tests.
"""
import json

import omsapi
import pytest
import requests

import faults

RUN = 398600


def _data(run=None):
    """The loops' query chain; `run` adds the run_number filter that scopes a fault."""
    query = omsapi.OMSAPI("https://cmsoms.cms/agg/api").query("runs")
    if run is not None:
        query = query.filter("run_number", run)
    return query.data()


def _runs(run=None):
    return len(_data(run).json()["data"])


@pytest.fixture
def one_run():
    omsapi.configure_runs([{"run_number": RUN, "end_time": None}])


@pytest.mark.parametrize(
    "mode, exc_cls",
    [("connection_error", requests.exceptions.ConnectionError), ("timeout", requests.exceptions.Timeout)],
)
def test_connection_errors_and_timeouts_raise_from_data(mode, exc_cls):
    omsapi.set_failure(mode)

    with pytest.raises(exc_cls, match=f"injected oms fault: mode={mode}"):
        _data()


def test_http_error_surfaces_only_through_raise_for_status(one_run):
    omsapi.set_failure("http_error", status=503)

    response = _data()  # the request itself "succeeds", like a real error response

    assert response.status_code == 503
    with pytest.raises(requests.exceptions.HTTPError, match="injected oms fault: mode=http_error status=503"):
        response.raise_for_status()


def test_malformed_json_surfaces_only_when_the_body_is_parsed(one_run):
    omsapi.set_failure("malformed_json")

    response = _data()
    response.raise_for_status()  # a 200 -- the body is what is wrong

    with pytest.raises(requests.exceptions.JSONDecodeError, match="injected oms fault: mode=malformed_json"):
        response.json()


def test_a_fault_is_single_shot_by_default(one_run):
    omsapi.set_failure("connection_error")

    with pytest.raises(requests.exceptions.ConnectionError):
        _data()
    assert _runs() == 1


def test_a_run_scoped_fault_only_hits_queries_filtering_on_that_run(one_run):
    omsapi.set_failure("connection_error", run=RUN, times=None)

    assert _runs() == 1  # no run_number filter at all, like a new-run query
    assert _runs(RUN + 1) == 0  # another run
    with pytest.raises(requests.exceptions.ConnectionError):
        _data(run=RUN)


def test_set_failure_validates_like_a_scenario_entry():
    with pytest.raises(faults.FaultSpecError, match="status"):
        omsapi.set_failure("http_error")


def test_a_fault_armed_in_the_live_fault_file_is_scoped_and_consumed(monkeypatch, tmp_path):
    """What seed.py's arm_fault() leaves for the loops' processes, re-read on every query."""
    path = tmp_path / "oms.json"
    faults.atomic_write_json(path, [{"mode": "connection_error", "times_remaining": 1, "run": RUN}])
    monkeypatch.setenv("NGT_FAULTS_DIR", str(tmp_path))

    assert _runs(RUN + 1) == 0  # another run: not consumed
    with pytest.raises(requests.exceptions.ConnectionError):
        _data(run=RUN)
    assert json.loads(path.read_text(encoding="utf-8")) == []
    assert _runs(RUN) == 0
