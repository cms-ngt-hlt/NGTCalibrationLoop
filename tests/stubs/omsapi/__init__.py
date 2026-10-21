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
- the NGT_OMS_STUB_RUNS_FILE env var -- used by scenario-player/live_demo.sh, points at a JSON file
  containing a list of run dicts that is re-read on every query, so a live NGTLoopStep2.py
  process picks up edits to the file (e.g. "end" a run) without needing a restart.
"""

import json
import os

_RUNS = []
_FAIL = False


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


def set_failure(should_fail):
    """Make the next query()...data() calls raise, to simulate OMS being unreachable."""
    global _FAIL
    _FAIL = should_fail


def reset():
    """Clear all configured runs/failure mode. Call between tests to avoid state leaking."""
    configure_runs([])
    set_failure(False)


def _current_runs():
    """The runs to query against: live-reloaded from NGT_OMS_STUB_RUNS_FILE if set
    (scenario-player/live_demo.sh usage), otherwise the in-memory list from configure_runs()
    (pytest usage)."""
    runs_file = os.environ.get("NGT_OMS_STUB_RUNS_FILE")
    if not runs_file:
        return _RUNS
    try:
        with open(runs_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"stub OMS response returned HTTP {self.status_code}")


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
        matched = self._matching_runs()
        payload = {"data": [{"attributes": dict(r)} for r in matched]}
        return _Response(payload)


class OMSAPI:
    """Drop-in stand-in for omsapi.OMSAPI, backed by `configure_runs()`."""

    def __init__(self, base_url, version="v1", cert_verify=True):
        self.base_url = base_url
        self.version = version
        self.cert_verify = cert_verify

    def query(self, entity):
        return _Query(entity)
