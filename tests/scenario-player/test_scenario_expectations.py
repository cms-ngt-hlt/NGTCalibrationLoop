"""
Tests for scenario-player/expectations.py. A "ledger" here is the list of rows
fake_conddb.read_ledger would return, built by `row()`, so no timing is involved.
"""
import pytest

import expectations as ngt_expectations  # scenario-player/expectations.py, via tests/conftest.py

CAL = "EcalPedestals"


def row(seq, lumisections, calibration=CAL, run=398600):
    return {"seq": seq, "calibration": calibration, "run": run, "lumisections": sorted(lumisections)}


def check(expect_block, rows):
    return ngt_expectations.evaluate(ngt_expectations.parse_expectations(expect_block), rows)


def one(lumisections, **extra):
    return {"calibration": CAL, "run": 398600, "lumisections": lumisections, **extra}


# --- lumisection specs ----------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        (51, {51}),
        ("51", {51}),
        ("51-53", {51, 52, 53}),
        ("51-53,57", {51, 52, 53, 57}),
        ("51 - 53", {51, 52, 53}),
        ([51, 52, 53], {51, 52, 53}),
        ([51, "60-62"], {51, 60, 61, 62}),
        ("70-70", {70}),
    ],
)
def test_parse_ls_spec_accepts_every_documented_spelling(spec, expected):
    assert ngt_expectations.parse_ls_spec(spec) == expected


@pytest.mark.parametrize("spec", [None, "", "abc", "53-51", "51-", "-51", True, 1.5, {}])
def test_parse_ls_spec_rejects_nonsense(spec):
    with pytest.raises(ValueError):
        ngt_expectations.parse_ls_spec(spec)


@pytest.mark.parametrize(
    "lumisections,expected",
    [({51, 52, 53}, "51-53"), ({51, 52, 53, 57}, "51-53,57"), ({51}, "51"), (set(), "(none)"), ({51, 53}, "51,53")],
)
def test_format_ls_set_collapses_consecutive_runs(lumisections, expected):
    assert ngt_expectations.format_ls_set(lumisections) == expected


def test_format_ls_set_round_trips_through_parse_ls_spec():
    original = {51, 52, 53, 57, 58, 90}
    assert ngt_expectations.parse_ls_spec(ngt_expectations.format_ls_set(original)) == original


# --- parsing -------------------------------------------------------------------


def test_parse_minimal_block_applies_documented_defaults():
    parsed = ngt_expectations.parse_expectations({"payloads": [one("51-53")]})

    assert parsed.settle_timeout == ngt_expectations.DEFAULT_SETTLE_TIMEOUT
    assert parsed.quiet_for == ngt_expectations.DEFAULT_QUIET_FOR
    assert parsed.ordered is False
    assert parsed.allow_duplicates is False
    assert (parsed.extra_payloads, parsed.extra_before, parsed.extra_between, parsed.extra_after) == (False,) * 4
    assert parsed.payloads[0].lumisections == frozenset({51, 52, 53})


def test_parse_scalar_allow_extra_sets_every_category():
    parsed = ngt_expectations.parse_expectations({"allow_extra": True, "payloads": [one(51)]})

    assert (parsed.extra_payloads, parsed.extra_before, parsed.extra_between, parsed.extra_after) == (True,) * 4


def test_parse_requires_payloads_and_says_how_to_mean_nothing():
    with pytest.raises(ngt_expectations.ExpectationError, match="payloads: \\[\\]"):
        ngt_expectations.parse_expectations({"allow_extra": False})


@pytest.mark.parametrize("flag", ["allow_duplicates", "allow_extra", "ordered", "quiet_for"])
def test_parse_points_a_misplaced_block_level_flag_back_up_a_level(flag):
    with pytest.raises(ngt_expectations.ExpectationError, match="move them up a level"):
        ngt_expectations.parse_expectations({"payloads": [one(51, **{flag: True})]})


@pytest.mark.parametrize(
    "block",
    [
        {"payloads": [one(51)], "nonsense": 1},
        {"payloads": [{"run": 398600, "lumisections": 51}]},
        {"payloads": [{"calibration": CAL, "lumisections": 51}]},
        {"payloads": [{"calibration": CAL, "run": "abc", "lumisections": 51}]},
        {"payloads": [one(51, bogus=True)]},
        {"payloads": [one(51)], "ordered": "yes"},
        {"payloads": [one(51)], "settle_timeout": 0},
        {"payloads": [one(51)], "allow_extra": {"sideways": True}},
        {"payloads": "not a list"},
        {"payloads": [one(None)]},
    ],
)
def test_parse_rejects_malformed_blocks(block):
    with pytest.raises(ngt_expectations.ExpectationError):
        ngt_expectations.parse_expectations(block)


# --- the happy paths -----------------------------------------------------------


def test_exact_single_payload_passes():
    verdict = check({"payloads": [one("51-53")]}, [row(1, [51, 52, 53])])
    assert verdict.ok, verdict.problems


def test_full_sequence_of_growing_reharvests_passes_when_all_listed():
    verdict = check(
        {"payloads": [one(51), one("51-52"), one("51-53")]},
        [row(1, [51]), row(2, [51, 52]), row(3, [51, 52, 53])],
    )
    assert verdict.ok, verdict.problems


def test_final_payload_idiom_tolerates_the_climb_but_not_a_later_upload():
    block = {"allow_extra": {"before": True}, "payloads": [one("51-53")]}

    assert check(block, [row(1, [51]), row(2, [51, 52]), row(3, [51, 52, 53])]).ok

    late = check(block, [row(1, [51]), row(2, [51, 52, 53]), row(3, [51, 52, 53, 54])])
    assert not late.ok
    assert any("after" in problem for problem in late.problems)


def test_empty_payloads_expects_an_empty_conditions_db():
    assert check({"payloads": []}, []).ok

    verdict = check({"payloads": []}, [row(1, [51])])
    assert not verdict.ok
    assert any("unexpected payload" in problem for problem in verdict.problems)


# --- lumisection-level tolerances ----------------------------------------------


def test_missing_lumisection_fails_and_names_it():
    verdict = check({"payloads": [one("51-54")]}, [row(1, [51, 52, 53])])
    assert not verdict.ok
    assert any("missing LS 54" in problem for problem in verdict.problems)


def test_extra_lumisection_fails_and_names_it():
    verdict = check({"payloads": [one("51-53")]}, [row(1, [51, 52, 53, 54])])
    assert not verdict.ok
    assert any("unexpected LS 54" in problem for problem in verdict.problems)


def test_allow_missing_lumisections_accepts_a_short_payload():
    assert check({"allow_missing_lumisections": True, "payloads": [one("51-54")]}, [row(1, [51, 52])]).ok


def test_allow_extra_lumisections_accepts_a_superset():
    assert check({"allow_extra_lumisections": True, "payloads": [one("51-52")]}, [row(1, [51, 52, 53])]).ok


def test_a_payload_that_never_arrived_is_reported_as_such():
    verdict = check({"payloads": [one("51-53")]}, [])
    assert not verdict.ok
    assert any("no matching upload" in problem for problem in verdict.problems)


# --- duplicates ----------------------------------------------------------------


def test_identical_lumisection_sets_are_a_duplicate_by_default():
    verdict = check(
        {"allow_extra": {"before": True}, "payloads": [one("51-53")]},
        [row(1, [51, 52, 53]), row(2, [51, 52, 53])],
    )
    assert not verdict.ok
    assert any("duplicate upload" in problem for problem in verdict.problems)


def test_growing_reharvests_are_not_duplicates():
    verdict = check(
        {"allow_extra": {"before": True}, "payloads": [one("51-53")]},
        [row(1, [51]), row(2, [51, 52]), row(3, [51, 52, 53])],
    )
    assert verdict.ok, verdict.problems


def test_allow_duplicates_alone_does_not_excuse_the_repeat_s_position():
    """A repeat is both a duplicate and an upload after the match: the two flags are independent."""
    verdict = check(
        {"allow_duplicates": True, "allow_extra": {"before": True}, "payloads": [one("51-53")]},
        [row(1, [51, 52, 53]), row(2, [51, 52, 53])],
    )
    assert not verdict.ok
    assert not any("duplicate" in problem for problem in verdict.problems)
    assert any("after" in problem for problem in verdict.problems)


def test_allow_duplicates_with_allow_extra_after_accepts_a_repeated_set():
    verdict = check(
        {"allow_duplicates": True, "allow_extra": {"before": True, "after": True}, "payloads": [one("51-53")]},
        [row(1, [51, 52, 53]), row(2, [51, 52, 53])],
    )
    assert verdict.ok, verdict.problems


# --- empty payloads ------------------------------------------------------------


def test_an_empty_payload_always_fails_even_where_extras_are_tolerated():
    verdict = check(
        {"allow_extra": True, "payloads": [one("51-53")]},
        [row(1, []), row(2, [51, 52, 53])],
    )
    assert not verdict.ok
    assert any("no lumisections" in problem for problem in verdict.problems)


def test_an_empty_payload_fails_even_when_it_is_the_only_upload():
    verdict = check({"payloads": []}, [row(1, [])])
    assert not verdict.ok
    assert any("no lumisections" in problem for problem in verdict.problems)


def test_no_empty_payload_complaint_when_every_upload_carries_lumisections():
    assert check({"payloads": [one("51-53")]}, [row(1, [51, 52, 53])]).ok


# --- extra-upload categories ---------------------------------------------------


@pytest.mark.parametrize(
    "rows,category",
    [
        ([row(1, [51]), row(2, [51, 52, 53])], "before"),
        ([row(1, [51, 52, 53]), row(2, [51, 52, 53, 54])], "after"),
    ],
)
def test_each_extra_category_is_named_in_the_report(rows, category):
    verdict = check({"payloads": [one("51-53")]}, rows)
    assert not verdict.ok
    assert any(category in problem for problem in verdict.problems), verdict.problems


def test_between_is_distinct_from_before_and_after():
    rows = [row(1, [51]), row(2, [51, 52]), row(3, [51, 52, 53])]
    block = {"allow_extra": {"before": True, "after": True}, "payloads": [one(51), one("51-53")]}

    verdict = check(block, rows)
    assert not verdict.ok
    assert any("between" in problem for problem in verdict.problems), verdict.problems

    block["allow_extra"]["between"] = True
    assert check(block, rows).ok


def test_an_unlisted_calibration_is_an_extra_payload_not_an_extra_upload():
    rows = [row(1, [51, 52, 53]), row(2, [51, 52, 53], calibration="BeamSpot")]
    block = {"payloads": [one("51-53")]}

    verdict = check(block, rows)
    assert not verdict.ok
    assert any("unexpected payload BeamSpot" in problem for problem in verdict.problems)

    block["allow_extra"] = {"payloads": True}
    assert check(block, rows).ok


def test_same_calibration_different_run_is_its_own_key():
    rows = [row(1, [51, 52, 53], run=398600), row(2, [51, 52, 53], run=398601)]
    assert check(
        {"payloads": [one("51-53"), {"calibration": CAL, "run": 398601, "lumisections": "51-53"}]}, rows
    ).ok


# --- ordering ------------------------------------------------------------------


def test_ordered_accepts_the_listed_order_across_calibrations():
    rows = [row(1, [51], calibration="EcalPedestals"), row(2, [51], calibration="BeamSpot")]
    block = {
        "ordered": True,
        "payloads": [
            {"calibration": "EcalPedestals", "run": 398600, "lumisections": 51},
            {"calibration": "BeamSpot", "run": 398600, "lumisections": 51},
        ],
    }
    assert check(block, rows).ok


def test_ordered_rejects_the_reverse_order():
    rows = [row(1, [51], calibration="BeamSpot"), row(2, [51], calibration="EcalPedestals")]
    block = {
        "ordered": True,
        "payloads": [
            {"calibration": "EcalPedestals", "run": 398600, "lumisections": 51},
            {"calibration": "BeamSpot", "run": 398600, "lumisections": 51},
        ],
    }
    verdict = check(block, rows)
    assert not verdict.ok
    assert any("out of the expected order" in problem for problem in verdict.problems)


def test_unordered_is_the_default_so_concurrent_calibrations_do_not_flake():
    rows = [row(1, [51], calibration="BeamSpot"), row(2, [51], calibration="EcalPedestals")]
    block = {
        "payloads": [
            {"calibration": "EcalPedestals", "run": 398600, "lumisections": 51},
            {"calibration": "BeamSpot", "run": 398600, "lumisections": 51},
        ]
    }
    assert check(block, rows).ok


def test_order_within_one_key_is_enforced_regardless_of_the_ordered_flag():
    verdict = check({"payloads": [one(51), one("51-52")]}, [row(1, [51, 52]), row(2, [51])])
    assert not verdict.ok


# --- reporting -----------------------------------------------------------------


def test_report_shows_the_ledger_and_every_problem():
    expectations = ngt_expectations.parse_expectations({"payloads": [one("51-54")]})
    verdict = ngt_expectations.evaluate(expectations, [row(1, [51, 52, 53])])
    report = ngt_expectations.format_report(verdict, expectations, name="demo.yaml")

    assert "demo.yaml expectations: FAILED" in report
    assert "seq 1" in report
    assert "missing LS 54" in report


def test_report_of_a_pass_says_passed_and_lists_the_uploads():
    expectations = ngt_expectations.parse_expectations({"payloads": [one("51-53")]})
    verdict = ngt_expectations.evaluate(expectations, [row(1, [51, 52, 53])])
    report = ngt_expectations.format_report(verdict, expectations, name="demo.yaml")

    assert "PASSED" in report
    assert "51-53" in report


def test_report_of_an_empty_expectation_is_readable():
    expectations = ngt_expectations.parse_expectations({"payloads": []})
    report = ngt_expectations.format_report(ngt_expectations.evaluate(expectations, []), expectations)

    assert "PASSED" in report
    assert "(empty)" in report


# --- settling ------------------------------------------------------------------


def test_wait_for_settle_returns_once_the_ledger_goes_quiet(monkeypatch):
    expectations = ngt_expectations.parse_expectations({"quiet_for": 5, "settle_timeout": 100, "payloads": []})

    now = [0.0]
    monkeypatch.setattr(ngt_expectations.fake_conddb, "read_ledger", lambda path=None: [])

    settled, rows = ngt_expectations.wait_for_settle(
        expectations,
        log=lambda *a: None,
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
        poll_interval=1,
    )

    assert settled is True
    assert rows == []
    assert now[0] >= 5  # it really did wait out quiet_for


def test_wait_for_settle_reports_not_settled_when_uploads_keep_arriving(monkeypatch):
    expectations = ngt_expectations.parse_expectations({"quiet_for": 5, "settle_timeout": 20, "payloads": []})

    now = [0.0]
    growing = []

    def ever_growing(path=None):
        growing.append(row(len(growing) + 1, [51]))
        return list(growing)

    monkeypatch.setattr(ngt_expectations.fake_conddb, "read_ledger", ever_growing)

    settled, rows = ngt_expectations.wait_for_settle(
        expectations,
        log=lambda *a: None,
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
        poll_interval=1,
    )

    assert settled is False
    assert len(rows) > 1
