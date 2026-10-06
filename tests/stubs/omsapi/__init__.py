"""
Minimal in-memory stand-in for the CERN oms-api-client (gitlab.cern.ch/cmsoms/oms-api-client).

The real client is not published on PyPI and requires CERN GitLab access, so it can't be
installed in a plain local/CI environment. NGTLoopStep2.py only relies on a small slice of
its query-builder interface (query().filter().sort().paginate().data().json()), which is
reimplemented here against an in-memory list of "runs" that tests populate directly.

This is a test double, not a general-purpose OMS client -- it has no network calls, no
authentication, and no real REST semantics beyond simple equality-filter/sort/paginate.

Two ways to feed it data:
- configure_runs() / reset() -- used by the pytest suite, in-memory only.
- the NGT_OMS_STUB_RUNS_FILE env var -- used by the live demos (exported by the $NGT_DEV_HOME/env.sh
  scenario-player/sim_env.sh writes), points at a JSON file
  containing a list of run dicts that is re-read on every query, so a live NGTLoopStep2.py
  process picks up edits to the file (e.g. "end" a run) without needing a restart.

Faults follow the same split: set_failure() in-process, $NGT_FAULTS_DIR/oms.json (written by
scenario-player/seed.py's arm_fault()) for the live demo.
"""

import json
import os

import requests

import faults as ngt_faults  # scenario-player/faults.py: on the path via env.sh and tests/conftest.py

_RUNS = []
_FAIL = False
_FAULTS = []  # set_failure()'s armed fault, as a fault-file entry (scenario-player/faults.py)

def configure_runs(runs):
    """Replace the in-memory set of OMS "runs" records returned by queries.

    Each run is a plain dict of attributes, e.g.:
        {"run_number": 398600, "fill_type_runtime": "PROTONS",
         "l1_hlt_mode": "collisions2026", "stable_beam": True,
         "start_time": "2026-08-10T10:00:00Z", "end_time": None,
         "last_lumisection_number": 120}
    """
    global _RUNS
    _RUNS = list(runs)


def set_failure(mode=True, *, status=None, times=1, run=None):
    """Simulate OMS trouble on the next query()...data() call(s).

    set_failure(True)/(False): every query raises RuntimeError / none does.
    set_failure("connection_error"|"timeout"|"http_error"|"malformed_json", ...): the
    `requests` exception a real outage raises, validated like a scenario's `faults:` entry
    (status for http_error; times=None for no limit; run= only hits queries filtering on
    that run_number, so never a search for a new run).
    """
    global _FAIL
    if mode is True or mode is False:
        _FAIL = mode
        _FAULTS.clear()
        return
    times = "unlimited" if times is None else times
    spec = ngt_faults.parse_fault_spec({"target": "oms", "mode": mode, "status": status, "times": times, "run": run})
    _FAIL = False
    _FAULTS[:] = [ngt_faults.fault_spec_to_entry(spec)]


def reset():
    """Clear all configured runs/failure mode. Call between tests to avoid state leaking."""
    configure_runs([])
    set_failure(False)


def _current_runs():
    """The runs to query against: live-reloaded from NGT_OMS_STUB_RUNS_FILE if set
    (live-demo usage), otherwise the in-memory list from configure_runs()
    (pytest usage)."""
    runs_file = os.environ.get("NGT_OMS_STUB_RUNS_FILE")
    if not runs_file:
        return _RUNS
    try:
        with open(runs_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _run_number_filter(query_filters):
    for key, value in query_filters:
        if key == "run_number":
            return value
    return None


def _consume_fault(run_number):
    """The armed fault this query hits, with one use counted off it: set_failure()'s,
    else one from $NGT_FAULTS_DIR/oms.json, re-read on every query like the runs file."""
    scope = {"run": run_number}
    fault = ngt_faults.take_fault(_FAULTS, scope)
    faults_dir = os.environ.get("NGT_FAULTS_DIR")
    if fault is None and faults_dir:
        path = os.path.join(faults_dir, "oms.json")
        with ngt_faults.locked_json_file(path):
            fault = ngt_faults.consume_fault(path, scope)
    return fault


class _Response:
    def __init__(self, payload, status_code=200, malformed_json=False, error=None):
        self._payload = payload
        self.status_code = status_code
        self._malformed_json = malformed_json
        self._error = error
        self.text = error if malformed_json else json.dumps(payload)

    def json(self):
        if self._malformed_json:
            raise requests.exceptions.JSONDecodeError(self._error, self.text, 0)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(self._error, response=self)


class _Query:
    def __init__(self, entity):
        self.entity = entity
        self._filters = []
        self._sort_key = None
        self._sort_asc = True
        self._page = 1
        self._per_page = None

    def filter(self, key, value):
        self._filters.append((key, value))
        return self

    def sort(self, key, asc=True):
        self._sort_key = key
        self._sort_asc = asc
        return self

    def paginate(self, page=1, per_page=None):
        self._page = page
        self._per_page = per_page
        return self

    def data_query(self):
        return f"<stub query on '{self.entity}' filters={self._filters}>"

    def _matching_runs(self):
        results = _current_runs()
        for key, value in self._filters:
            results = [r for r in results if r.get(key) == value]
        if self._sort_key:
            results = sorted(
                results, key=lambda r: r.get(self._sort_key), reverse=not self._sort_asc
            )
        if self._per_page:
            start = (self._page - 1) * self._per_page
            results = results[start : start + self._per_page]
        return results

    def data(self):
        if _FAIL:
            raise RuntimeError("stub OMS is configured to fail (omsapi.set_failure(True))")

        run_number = _run_number_filter(self._filters)
        fault = _consume_fault(run_number)
        error = None
        if fault:
            error = ngt_faults.describe("oms", fault)
            if fault["mode"] in ("connection_error", "timeout"):
                cls = requests.exceptions.ConnectionError if fault["mode"] == "connection_error" else requests.exceptions.Timeout
                raise cls(error)

        matched = self._matching_runs()
        payload = {"data": [{"attributes": dict(r)} for r in matched]}

        status = fault["status"] if fault and fault["mode"] == "http_error" else 200
        malformed = bool(fault) and fault["mode"] == "malformed_json"
        return _Response(payload, status_code=status, malformed_json=malformed, error=error)


class OMSAPI:
    """Drop-in stand-in for omsapi.OMSAPI, backed by `configure_runs()`."""

    def __init__(self, base_url, version="v1", cert_verify=True):
        self.base_url = base_url
        self.version = version
        self.cert_verify = cert_verify

    def query(self, entity):
        return _Query(entity)
