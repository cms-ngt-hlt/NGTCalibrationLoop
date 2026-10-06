"""A scenario's `expect:` block -- the payloads that should reach the dummy conditions DB
(scenario-player/fake_conddb.py) -- and the check that turns it into a pass/fail verdict:

    expect:
      settle_timeout: 300          # cap on waiting for uploads to arrive
      quiet_for: 20                # settled once no new upload lands for this long
      allow_missing_lumisections: false   # expected LS absent from a matched upload
      allow_extra_lumisections: false     # unexpected LS present in one
      allow_duplicates: false             # two uploads carrying an identical LS set
      ordered: false                      # listed entries must land in the listed order
      allow_extra:                 # unlisted *uploads*; scalar true/false sets all four
        payloads: false            #   a (calibration, run) appearing in no entry at all
        before: false              #   extra uploads for a listed key, before its first match
        between: false             #   ... interleaved between its matches
        after: false               #   ... after its last match
      payloads:
        - {calibration: EcalPedestals, run: 398600, lumisections: 51-53}

`payloads:` is the expected sequence of uploads: every ledger row must be matched by an entry or
allowed by `allow_extra`. The flags apply to the whole block; README.md's "Validating the result"
says why each exists.
"""
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import fake_conddb

DEFAULT_SETTLE_TIMEOUT = 300.0
DEFAULT_QUIET_FOR = 20.0
DEFAULT_POLL_INTERVAL = 2.0

EXTRA_CATEGORIES = ("payloads", "before", "between", "after")
# The block's own flags: written inside a `payloads` entry, they get an error saying where they go.
BLOCK_LEVEL_FLAGS = frozenset(
    {
        "allow_missing_lumisections",
        "allow_extra_lumisections",
        "allow_duplicates",
        "allow_extra",
        "ordered",
        "settle_timeout",
        "quiet_for",
    }
)


class ExpectationError(ValueError):
    """Raised for an invalid `expect:` block, naming the offending field."""


def _require(condition, message):
    if not condition:
        raise ExpectationError(message)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_bool(value, name):
    _require(isinstance(value, bool), f"expect: {name!r} must be true or false, got {value!r}")
    return value


def _require_positive_number(value, name):
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0,
        f"expect: {name!r} must be a positive number, got {value!r}",
    )
    return float(value)


def _positive_ls(value):
    # LS numbering starts at 1; a stray leading '-' would otherwise parse and never match.
    if value < 1:
        raise ValueError(f"lumisections: {value} is not a valid lumisection number (they start at 1)")
    return value


def parse_ls_spec(spec):
    """A `lumisections:` value -> a set of ints: `51`, `"51-53"`, `"51-53,57"`, or a list of
    numbers and ranges (`[51, "60-62"]`). Ranges are inclusive, as everywhere in CMS."""
    if spec is None:
        raise ValueError("lumisections: missing (expected e.g. 51, '51-53' or [51, 52, 53])")
    if isinstance(spec, bool):
        raise ValueError(f"lumisections: expected numbers or ranges, got {spec!r}")
    if isinstance(spec, int):
        return {_positive_ls(spec)}
    if isinstance(spec, (list, tuple, set)):
        return set().union(*(parse_ls_spec(item) for item in spec))
    if not isinstance(spec, str):
        raise ValueError(f"lumisections: expected a number, range string or list, got {spec!r}")

    merged = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            low_raw, _, high_raw = part.partition("-")
            try:
                low, high = int(low_raw), int(high_raw)
            except ValueError:
                raise ValueError(f"lumisections: {part!r} is not a valid range (expected e.g. '51-53')")
            if high < low:
                raise ValueError(f"lumisections: range {part!r} ends before it starts")
            merged.update(range(_positive_ls(low), _positive_ls(high) + 1))
        else:
            try:
                value = int(part)
            except ValueError:
                raise ValueError(f"lumisections: {part!r} is not a lumisection number")
            merged.add(_positive_ls(value))
    if not merged:
        raise ValueError(f"lumisections: {spec!r} describes no lumisections")
    return merged


def format_ls_set(lumisections):
    """{51, 52, 53, 57} -> "51-53,57": parse_ls_spec's inverse, for reports."""
    remaining = sorted(set(lumisections))
    if not remaining:
        return "(none)"
    groups = []
    start = previous = remaining[0]
    for value in remaining[1:]:
        if value == previous + 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    return ",".join(str(low) if low == high else f"{low}-{high}" for low, high in groups)


@dataclass(frozen=True)
class PayloadExpectation:
    calibration: str
    run: int
    lumisections: frozenset
    index: int = 0  # position in the `payloads:` list, for `ordered`

    @property
    def key(self):
        return (self.calibration, self.run)

    def describe(self):
        return f"{self.calibration}/run{self.run} LS {format_ls_set(self.lumisections)}"

    def compare(self, row) -> Tuple[set, set]:
        """(missing, extra) lumisections for one candidate ledger row."""
        actual = _row_ls(row)
        return set(self.lumisections) - actual, actual - set(self.lumisections)

    def accepts(self, row, expectations) -> bool:
        missing, extra = self.compare(row)
        if missing and not expectations.allow_missing_lumisections:
            return False
        if extra and not expectations.allow_extra_lumisections:
            return False
        return True


@dataclass(frozen=True)
class Expectations:
    payloads: tuple = ()
    allow_missing_lumisections: bool = False
    allow_extra_lumisections: bool = False
    allow_duplicates: bool = False
    extra_payloads: bool = False
    extra_before: bool = False
    extra_between: bool = False
    extra_after: bool = False
    ordered: bool = False
    settle_timeout: float = DEFAULT_SETTLE_TIMEOUT
    quiet_for: float = DEFAULT_QUIET_FOR

    def describe(self):
        if not self.payloads:
            return "expect: no payloads should reach the conditions DB"
        return "expect: " + "; ".join(entry.describe() for entry in self.payloads)


def _parse_allow_extra(raw):
    """-> (payloads, before, between, after). A scalar sets all four."""
    if raw is None:
        return (False, False, False, False)
    if isinstance(raw, bool):
        return (raw, raw, raw, raw)
    _require(
        isinstance(raw, dict),
        f"expect: 'allow_extra' must be true/false or a map of {list(EXTRA_CATEGORIES)}, got {raw!r}",
    )
    raw = dict(raw)
    values = [_require_bool(raw.pop(category, False), f"allow_extra.{category}") for category in EXTRA_CATEGORIES]
    _require(not raw, f"expect: unknown 'allow_extra' categor(ies) {sorted(raw)}; valid: {list(EXTRA_CATEGORIES)}")
    return tuple(values)


def _parse_payload(entry_raw, index):
    _require(isinstance(entry_raw, dict), f"expect: payloads[{index}] must be a mapping, got {entry_raw!r}")
    entry_raw = dict(entry_raw)
    where = f"payloads[{index}]"

    calibration = entry_raw.pop("calibration", None)
    _require(
        isinstance(calibration, str) and calibration,
        f"expect: {where} requires a 'calibration' (e.g. EcalPedestals)",
    )
    run = entry_raw.pop("run", None)
    _require(_as_int(run) is not None, f"expect: {where} requires an integer 'run', got {run!r}")

    try:
        lumisections = parse_ls_spec(entry_raw.pop("lumisections", None))
    except ValueError as exc:
        raise ExpectationError(f"expect: {where} {exc}") from exc

    misplaced = sorted(set(entry_raw) & BLOCK_LEVEL_FLAGS)
    _require(
        not misplaced,
        f"expect: {where} sets {misplaced}, which apply to the whole expect: block rather than one "
        "payload -- move them up a level",
    )
    _require(not entry_raw, f"expect: unknown field(s) {sorted(entry_raw)} in {where}")

    return PayloadExpectation(
        calibration=calibration,
        run=int(run),
        lumisections=frozenset(lumisections),
        index=index,
    )


def parse_expectations(raw: dict) -> Expectations:
    """Validate an `expect:` block (a plain dict from YAML) into Expectations, raising
    ExpectationError, naming the field, on anything invalid -- a typo fails fast instead of
    quietly weakening the check."""
    _require(isinstance(raw, dict), f"expect: must be a mapping, got {raw!r}")
    raw = dict(raw)

    settle_timeout = _require_positive_number(raw.pop("settle_timeout", DEFAULT_SETTLE_TIMEOUT), "settle_timeout")
    quiet_for = _require_positive_number(raw.pop("quiet_for", DEFAULT_QUIET_FOR), "quiet_for")
    ordered = _require_bool(raw.pop("ordered", False), "ordered")
    allow_missing = _require_bool(raw.pop("allow_missing_lumisections", False), "allow_missing_lumisections")
    allow_extra_ls = _require_bool(raw.pop("allow_extra_lumisections", False), "allow_extra_lumisections")
    allow_duplicates = _require_bool(raw.pop("allow_duplicates", False), "allow_duplicates")
    extra_payloads, extra_before, extra_between, extra_after = _parse_allow_extra(raw.pop("allow_extra", None))

    payloads_raw = raw.pop("payloads", None)
    _require(
        payloads_raw is not None,
        "expect: 'payloads' is required -- use 'payloads: []' to assert that nothing should be uploaded",
    )
    _require(isinstance(payloads_raw, list), f"expect: 'payloads' must be a list, got {payloads_raw!r}")

    _require(not raw, f"expect: unknown field(s) {sorted(raw)}")

    return Expectations(
        payloads=tuple(_parse_payload(entry_raw, index) for index, entry_raw in enumerate(payloads_raw)),
        allow_missing_lumisections=allow_missing,
        allow_extra_lumisections=allow_extra_ls,
        allow_duplicates=allow_duplicates,
        extra_payloads=extra_payloads,
        extra_before=extra_before,
        extra_between=extra_between,
        extra_after=extra_after,
        ordered=ordered,
        settle_timeout=settle_timeout,
        quiet_for=quiet_for,
    )


@dataclass
class Verdict:
    ok: bool
    problems: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    settled: bool = True


def _row_key(row):
    return (row.get("calibration"), row.get("run"))


def _row_ls(row):
    return set(row.get("lumisections") or [])


def _describe_row(row):
    return (
        f"seq {row.get('seq')}  {row.get('calibration')}/run{row.get('run')}  "
        f"LS {format_ls_set(_row_ls(row))}"
    )


def _group_rows(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(_row_key(row), []).append(row)
    for key_rows in grouped.values():
        key_rows.sort(key=lambda row: row.get("seq") or 0)
    return grouped


def _align(entries, key_rows, expectations, problems):
    """Match each entry, in listed order, to the next row that satisfies it; returns
    {row index -> entry}. A row skipped while searching stays unmatched (an extra); the cursor
    never moves back, which is what orders the entries of one (calibration, run)."""
    matches = {}
    cursor = 0
    for entry in entries:
        found = None
        for i in range(cursor, len(key_rows)):
            if entry.accepts(key_rows[i], expectations):
                found = i
                break
        if found is None:
            problems.append(_missing_problem(entry, key_rows[cursor:]))
            continue
        matches[found] = entry
        cursor = found + 1
    return matches


def _missing_problem(entry, candidate_rows):
    """Why no upload satisfied `entry`, quoting the closest candidate's difference."""
    if not candidate_rows:
        return f"{entry.describe()}: no matching upload (none left for this run)"
    differences = [(row, entry.compare(row)) for row in candidate_rows]
    best, (missing, extra) = min(differences, key=lambda pair: len(pair[1][0]) + len(pair[1][1]))
    bits = []
    if missing:
        bits.append(f"missing LS {format_ls_set(missing)}")
    if extra:
        bits.append(f"unexpected LS {format_ls_set(extra)}")
    detail = "; ".join(bits) or "no difference in lumisections"
    return f"{entry.describe()}: no matching upload -- closest was seq {best.get('seq')} ({detail})"


def _classify_extras(matches, key_rows, expectations, problems):
    if not matches:
        return
    first, last = min(matches), max(matches)
    for i, row in enumerate(key_rows):
        if i in matches:
            continue
        if i < first:
            category, allowed = "before", expectations.extra_before
        elif i > last:
            category, allowed = "after", expectations.extra_after
        else:
            category, allowed = "between", expectations.extra_between
        if not allowed:
            problems.append(
                f"unexpected upload {category} the expected payload(s): {_describe_row(row)} "
                f"(allow with allow_extra.{category})"
            )


def _check_duplicates(key_rows, expectations, problems):
    """A duplicate is the *same* LS set twice for one (calibration, run) -- an upload with nothing
    new behind it -- not a growing re-harvest."""
    if expectations.allow_duplicates:
        return
    seen = {}
    for row in key_rows:
        signature = frozenset(_row_ls(row))
        if signature in seen:
            problems.append(
                f"duplicate upload: seq {row.get('seq')} repeats the lumisection set already uploaded at "
                f"seq {seen[signature]} ({row.get('calibration')}/run{row.get('run')} "
                f"LS {format_ls_set(signature)}) -- allow with allow_duplicates: true"
            )
        else:
            seen[signature] = row.get("seq")


def _check_empty_payloads(rows, problems):
    """A payload covering no lumisections fails whatever the flags: a harvest only runs when there
    is something to harvest, so it means a broken upload, not an extra."""
    for row in rows:
        if not _row_ls(row):
            problems.append(
                f"payload with no lumisections: {_describe_row(row)} -- a harvest only runs when "
                "there is something to harvest, so either nothing of substance was uploaded or the "
                ".db's provenance could not be read (check that job's upload_stderr.log)"
            )


def _check_order(all_matches, problems):
    """`ordered` only governs entries of *different* keys; _align already orders each key's."""
    in_listed_order = [seq for _, seq in sorted(all_matches, key=lambda pair: pair[0])]
    for earlier, later in zip(in_listed_order, in_listed_order[1:]):
        if later < earlier:
            problems.append(
                f"uploads landed out of the expected order: an entry matched seq {later} after an "
                f"earlier entry matched seq {earlier} (relax with ordered: false)"
            )
            return


def evaluate(expectations: Expectations, rows) -> Verdict:
    """Check ledger rows (as fake_conddb.read_ledger returns them) against Expectations."""
    problems = []
    _check_empty_payloads(rows, problems)
    rows_by_key = _group_rows(rows)

    entries_by_key = {}
    for entry in expectations.payloads:
        entries_by_key.setdefault(entry.key, []).append(entry)

    all_matches = []
    for key, entries in entries_by_key.items():
        key_rows = rows_by_key.get(key, [])
        matches = _align(entries, key_rows, expectations, problems)
        _classify_extras(matches, key_rows, expectations, problems)
        _check_duplicates(key_rows, expectations, problems)
        for row_index, entry in matches.items():
            all_matches.append((entry.index, key_rows[row_index].get("seq") or 0))

    for key, key_rows in rows_by_key.items():
        if key in entries_by_key or expectations.extra_payloads:
            continue
        calibration, run = key
        problems.append(
            f"unexpected payload {calibration}/run{run}: {len(key_rows)} upload(s) reached the "
            f"conditions DB but no expect: entry describes it (allow with allow_extra.payloads) -- "
            f"first was {_describe_row(key_rows[0])}"
        )

    if expectations.ordered:
        _check_order(all_matches, problems)

    return Verdict(ok=not problems, problems=problems, rows=list(rows))


def wait_for_settle(
    expectations: Expectations,
    log=print,
    sleep=time.sleep,
    clock=time.monotonic,
    poll_interval=DEFAULT_POLL_INTERVAL,
    skip=0,
):
    """Poll the ledger until no upload has landed for quiet_for, or settle_timeout passes.
    Returns (settled, rows): the rows after the first `skip`, and False if still arriving.

    Waiting for quiet, not for the expectations to hold, is what lets `allow_extra.after: false`
    catch a late upload; a passing scenario pays with its last quiet_for seconds."""
    path = fake_conddb.ledger_path()
    started = last_change = clock()
    rows = fake_conddb.read_ledger(path)[skip:]
    log(f"Waiting for uploads to settle ({len(rows)} so far; quiet_for={expectations.quiet_for:.0f}s, "
        f"timeout={expectations.settle_timeout:.0f}s) -- {path}")

    while True:
        now = clock()
        if now - last_change >= expectations.quiet_for:
            return True, rows
        if now - started >= expectations.settle_timeout:
            return False, rows
        sleep(min(poll_interval, expectations.quiet_for))
        latest = fake_conddb.read_ledger(path)[skip:]
        if len(latest) != len(rows):
            for row in latest[len(rows):]:
                log(f"  upload: {_describe_row(row)}")
            rows, last_change = latest, clock()


def format_report(verdict: Verdict, expectations: Optional[Expectations] = None, name="Scenario") -> str:
    """A human-readable report: the uploads checked, then what was wrong."""
    lines = []
    status = "PASSED" if verdict.ok else "FAILED"
    if not verdict.settled:
        status = "FAILED (timed out waiting for uploads to settle)"
    lines.append(f"{name} expectations: {status}")

    if expectations is not None:
        lines.append(f"  {expectations.describe()}")

    lines.append(f"  conditions DB: {len(verdict.rows)} upload(s) checked, from {fake_conddb.ledger_path()}")
    for row in verdict.rows:
        lines.append(f"    {_describe_row(row)}")
    if not verdict.rows:
        lines.append("    (empty)")

    if verdict.problems:
        lines.append(f"  {len(verdict.problems)} problem(s):")
        for problem in verdict.problems:
            lines.append(f"    - {problem}")
    return "\n".join(lines)
