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
- the NGT_OMS_STUB_RUNS_FILE env var -- used by the live demo (airflow_demo.sh), points at a JSON file
  containing a list of run dicts that is re-read on every query, so a live NGTLoopStep2.py
  process picks up edits to the file (e.g. "end" a run) without needing a restart.

Failure injection mirrors that same in-memory/file-based split -- see set_failure() for
pytest, and NGT_OMS_STUB_FAULTS_FILE (scenario-player/faults.py's arm/consume helpers,
written by scenario-player/seed.py's arm_fault()) for the live demo, whose Airflow process is separate
from whatever process ran scenario-player/scenario_player.py.
"""

import json
import os

import requests

_RUNS = []
_FAIL = False
_FAULT = None  # {"mode", "status", "times_remaining", "run", "message"} | None

_OMS_FAULT_MODES = ("connection_error", "timeout", "http_error", "malformed_json")

_HTTP_REASONS = {
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


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


def set_failure(mode=True, *, status=None, times=1, run=None, message=None):
    """Make the next query()...data() call(s) raise, to simulate OMS trouble.

    set_failure(True) / set_failure(False) is the original, still-supported form:
    an unconditional, persistent (non-counting) failure/no-failure toggle -- every
    existing caller uses exactly this form and keeps working unchanged.

    set_failure(mode=<one of "connection_error"|"timeout"|"http_error"|"malformed_json">, ...)
    is the richer form used by tests/test_oms_faults.py and the `inject_fault` fixture:
      - mode picks which *real* requests exception .data()/.raise_for_status()/.json()
        raises, so oms.py's actual except clauses (which key off real exception types,
        not a generic RuntimeError) are genuinely exercised.
      - status is required for mode="http_error" (an int 400-599).
      - times limits how many matching calls fail before the fault clears itself
        (default 1 = single-shot; None = unlimited, matching FaultSpec.times).
      - run optionally scopes the fault to only queries that filter on that
        run_number (several oms.py functions do; find_new_run never does, so an
        unscoped -- or run-scoped -- fault is the only way to ever hit it).
      - message optionally overrides the canned realistic error text (see
        _default_message), e.g. so a scenario's Airflow task logs show
        deliberately chosen wording.
    """
    global _FAIL, _FAULT
    if mode is False:
        _FAIL, _FAULT = False, None
        return
    if mode is True:
        _FAIL, _FAULT = True, None
        return
    if mode not in _OMS_FAULT_MODES:
        raise ValueError(f"unknown OMS fault mode {mode!r} -- must be one of {_OMS_FAULT_MODES}")
    if mode == "http_error" and not (isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599):
        raise ValueError("set_failure(mode='http_error', ...) requires status=<int 400-599>")
    _FAIL = False
    _FAULT = {"mode": mode, "status": status, "times_remaining": times, "run": run, "message": message}


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


def _consume_inmemory_fault(run_number):
    """Pop and return the in-memory fault if it's armed and its (optional)
    `run` scope matches this call's run_number filter (a query with no
    run_number filter at all, e.g. find_new_run, only matches an unscoped
    fault) -- decrementing times_remaining, clearing it once exhausted."""
    global _FAULT
    if _FAULT is None:
        return None
    if _FAULT["run"] is not None and _FAULT["run"] != run_number:
        return None
    matched = dict(_FAULT)
    remaining = _FAULT["times_remaining"]
    if remaining is not None:
        remaining -= 1
        if remaining <= 0:
            _FAULT = None
        else:
            _FAULT["times_remaining"] = remaining
    return matched


def _consume_file_fault(run_number):
    """The live-demo equivalent of _consume_inmemory_fault: scenario-player/seed.py's
    arm_fault() (called from scenario-player/scenario_player.py, a separate OS process
    from Airflow) writes armed faults to $NGT_DEV_HOME/faults/oms.json;
    Airflow's own process (this module, imported by oms.py) polls it here on
    every query, exactly mirroring how _current_runs() already re-reads
    NGT_OMS_STUB_RUNS_FILE across that same process boundary."""
    faults_file = os.environ.get("NGT_OMS_STUB_FAULTS_FILE")
    if not faults_file:
        return None
    # scenario-player/faults.py: on PYTHONPATH via scenario-player/sim_env.sh in the
    # live demo, and via tests/conftest.py's sys.path setup under pytest.
    import faults as ngt_faults

    with ngt_faults.locked_json_file(faults_file):
        return ngt_faults.consume_fault(faults_file, {"run": run_number})


def _default_message(mode, status=None):
    """Realistic-looking canned error text per fault mode, so a task log
    someone is debugging (e.g. via the Airflow UI) shows something that
    resembles a genuine OMS outage rather than a bare placeholder."""
    if mode == "connection_error":
        return (
            "HTTPSConnectionPool(host='cmsoms.cms', port=443): Max retries exceeded with "
            "url: /agg/api/v1/runs (Caused by NewConnectionError('<urllib3.connection."
            "HTTPSConnection object>: Failed to establish a new connection: "
            "[Errno 111] Connection refused'))"
        )
    if mode == "timeout":
        return "HTTPSConnectionPool(host='cmsoms.cms', port=443): Read timed out. (read timeout=None)"
    if mode == "http_error":
        kind = "Server" if status is not None and status >= 500 else "Client"
        reason = _HTTP_REASONS.get(status, "Error")
        return f"{status} {kind} Error: {reason} for url: https://cmsoms.cms/agg/api/v1/runs"
    if mode == "malformed_json":
        return "Expecting value: line 1 column 1 (char 0)"
    return "stub OMS simulated failure"


class _Response:
    def __init__(self, payload, status_code=200, malformed_json=False, fault_message=None):
        self._payload = payload
        self.status_code = status_code
        self._malformed_json = malformed_json
        self._fault_message = fault_message
        if malformed_json:
            # A realistic real-world failure mode: OMS (or a load balancer/
            # proxy in front of it) returns an HTML error page instead of
            # JSON when it's unhealthy.
            self.text = (
                "<html><head><title>503 Service Unavailable</title></head>"
                "<body><h1>Service Unavailable</h1><p>"
                f"{fault_message or 'The server is temporarily unable to service your request.'}"
                "</p></body></html>"
            )
        else:
            self.text = json.dumps(payload)

    def json(self):
        if self._malformed_json:
            raise requests.exceptions.JSONDecodeError(
                self._fault_message or "Expecting value: line 1 column 1 (char 0)", self.text, 0
            )
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                self._fault_message or f"stub OMS response returned HTTP {self.status_code}",
                response=self,
            )


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
        fault = _consume_inmemory_fault(run_number) or _consume_file_fault(run_number)

        if fault and fault["mode"] in ("connection_error", "timeout"):
            cls = requests.exceptions.ConnectionError if fault["mode"] == "connection_error" else requests.exceptions.Timeout
            raise cls(fault.get("message") or _default_message(fault["mode"]))

        matched = self._matching_runs()
        payload = {"data": [{"attributes": dict(r)} for r in matched]}

        status, malformed, fault_message = 200, False, None
        if fault and fault["mode"] == "http_error":
            status = fault["status"]
            fault_message = fault.get("message") or _default_message("http_error", status)
        elif fault and fault["mode"] == "malformed_json":
            malformed = True
            fault_message = fault.get("message") or _default_message("malformed_json")

        return _Response(payload, status_code=status, malformed_json=malformed, fault_message=fault_message)


class OMSAPI:
    """Drop-in stand-in for omsapi.OMSAPI, backed by `configure_runs()`."""

    def __init__(self, base_url, version="v1", cert_verify=True):
        self.base_url = base_url
        self.version = version
        self.cert_verify = cert_verify

    def query(self, entity):
        return _Query(entity)
