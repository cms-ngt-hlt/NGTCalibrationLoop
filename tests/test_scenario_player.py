"""
Tests for scenario-player/scenario_player.py -- the timed, engine-agnostic run/lumisection
playback tool (see scenario-player/scenarios/*.yaml for example scenario files). These
tests never touch $NGT_DEV_HOME or the filesystem: scenario-player/seed.py's actual
seed_run/add_ls/end_run are monkeypatched out so we can assert purely on
event ordering/timing and on what scenario_player would have called.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_PLAYER_DIR = REPO_ROOT / "scenario-player"
if str(SCENARIO_PLAYER_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIO_PLAYER_DIR))

import scenario_player  # noqa: E402


def test_build_timeline_single_run_orders_events_by_cumulative_delay():
    scenario = {
        "runs": [
            {
                "calibrations": ["EcalPedestals"],
                "run": 398600,
                "start_offset": 5,
                "lumisections": [
                    {"ls": 51, "after": 0},
                    {"ls": 52, "after": 20},
                ],
                "end_after": 30,
            }
        ]
    }
    events = scenario_player.build_timeline(scenario)
    kinds_and_times = [(e.kind, e.at, e.ls) for e in events]
    assert kinds_and_times == [
        ("start_run", 5, None),
        ("add_ls", 5, 51),   # after=0 -> same timestamp as start
        ("add_ls", 25, 52),  # 5 + 0 + 20
        ("end_run", 55, None),  # 25 + 30
    ]
    assert all(e.calibration == "EcalPedestals" and e.run == 398600 for e in events)


def test_build_timeline_merges_sequential_runs_by_absolute_time():
    # Runs must not overlap (see test_build_timeline_rejects_overlapping_runs)
    # -- this covers that multiple, non-overlapping run specs still merge
    # into one correctly-ordered global timeline.
    scenario = {
        "runs": [
            {"calibrations": ["SiStripBad"], "run": 1, "start_offset": 0, "end_after": 10},
            {"calibrations": ["SiStripBad"], "run": 2, "start_offset": 20, "end_after": 5},
        ]
    }
    events = scenario_player.build_timeline(scenario)
    assert [(e.run, e.kind, e.at) for e in events] == [
        (1, "start_run", 0),
        (1, "end_run", 10),
        (2, "start_run", 20),
        (2, "end_run", 25),
    ]


def test_build_timeline_rejects_overlapping_runs():
    # A run is one CMS-wide DAQ run -- there is only ever one active at a
    # time, never two different run numbers concurrently. Multiple
    # calibrations watching the *same* run at once belongs under one run's
    # 'calibrations' list instead (see the next test).
    scenario = {
        "runs": [
            {"calibrations": ["SiStripBad"], "run": 1, "start_offset": 0, "end_after": 100},
            {"calibrations": ["EcalPedestals"], "run": 2, "start_offset": 10, "end_after": 5},
        ]
    }
    with pytest.raises(ValueError, match="overlapping"):
        scenario_player.build_timeline(scenario)


def test_build_timeline_one_run_multiple_calibrations_shares_one_run_number():
    scenario = {
        "runs": [
            {
                "calibrations": ["EcalPedestals", "SiStripBad"],
                "run": 500,
                "start_offset": 0,
                "lumisections": [{"ls": 1, "after": 0}],
                "end_after": 5,
            }
        ]
    }
    events = scenario_player.build_timeline(scenario)
    assert all(e.run == 500 for e in events)
    start_calibrations = {e.calibration for e in events if e.kind == "start_run"}
    add_ls_calibrations = {e.calibration for e in events if e.kind == "add_ls"}
    end_events = [e for e in events if e.kind == "end_run"]
    assert start_calibrations == {"EcalPedestals", "SiStripBad"}
    assert add_ls_calibrations == {"EcalPedestals", "SiStripBad"}
    # end-run isn't calibration-specific (scenario-player/seed.py's end_run takes no
    # calibration argument -- it flips one shared OMS record) -- fired once
    # per run spec, not once per listed calibration.
    assert len(end_events) == 1


def test_build_timeline_rejects_missing_calibrations():
    with pytest.raises(ValueError, match="calibrations"):
        scenario_player.build_timeline({"runs": [{"run": 1, "end_after": 1}]})


def test_build_timeline_rejects_bare_string_calibrations():
    with pytest.raises(ValueError, match="calibrations"):
        scenario_player.build_timeline({"runs": [{"calibrations": "EcalPedestals", "run": 1, "end_after": 1}]})


def test_build_timeline_empty_scenario_returns_no_events():
    assert scenario_player.build_timeline({"runs": []}) == []
    assert scenario_player.build_timeline({}) == []


def test_dry_run_prints_timeline_without_firing_events(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "seed_run", lambda *a, **kw: calls.append(("seed_run", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "add_ls", lambda *a, **kw: calls.append(("add_ls", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "end_run", lambda *a, **kw: calls.append(("end_run", a, kw)))

    events = scenario_player.build_timeline(
        {"runs": [{"calibrations": ["BeamSpot"], "run": 7, "lumisections": [{"ls": 1, "after": 0}], "end_after": 1}]}
    )
    lines = []
    scenario_player.play(events, dry_run=True, log=lines.append)

    assert calls == []  # dry-run must not touch the fake OMS/EOS state at all
    assert len(lines) == len(events) == 3
    assert "seed-run" in lines[0]
    assert "add-ls" in lines[1]
    assert "end-run" in lines[2]


def test_play_fires_events_in_order_with_expected_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "seed_run", lambda *a, **kw: calls.append(("seed_run", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "add_ls", lambda *a, **kw: calls.append(("add_ls", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "end_run", lambda *a, **kw: calls.append(("end_run", a, kw)))
    # Real playback would sleep between events; skip that entirely so the test
    # is instant and deterministic -- ordering/arguments are what's under test.
    monkeypatch.setattr(scenario_player.time, "sleep", lambda _seconds: None)

    scenario = {
        "runs": [
            {
                "calibrations": ["EcalPedestals"],
                "run": 398600,
                "minutes_ago": 3,
                "lumisections": [{"ls": 51, "after": 0}, {"ls": 52, "after": 20}],
                "end_after": 10,
            }
        ]
    }
    events = scenario_player.build_timeline(scenario)
    scenario_player.play(events, speed=1.0, log=lambda _msg: None)

    assert calls == [
        ("seed_run", ("EcalPedestals", 398600), {"minutes_ago": 3.0, "ls": []}),
        ("add_ls", ("EcalPedestals", 398600, 51), {}),
        ("add_ls", ("EcalPedestals", 398600, 52), {}),
        ("end_run", (398600,), {}),
    ]


def test_play_fires_one_seed_run_and_add_ls_per_calibration_but_one_end_run(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "seed_run", lambda *a, **kw: calls.append(("seed_run", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "add_ls", lambda *a, **kw: calls.append(("add_ls", a, kw)))
    monkeypatch.setattr(scenario_player.seed, "end_run", lambda *a, **kw: calls.append(("end_run", a, kw)))
    monkeypatch.setattr(scenario_player.time, "sleep", lambda _seconds: None)

    scenario = {
        "runs": [
            {
                "calibrations": ["EcalPedestals", "SiStripBad"],
                "run": 500,
                "lumisections": [{"ls": 1, "after": 0}],
                "end_after": 5,
            }
        ]
    }
    events = scenario_player.build_timeline(scenario)
    scenario_player.play(events, log=lambda _msg: None)

    seed_run_calls = [c for c in calls if c[0] == "seed_run"]
    add_ls_calls = [c for c in calls if c[0] == "add_ls"]
    end_run_calls = [c for c in calls if c[0] == "end_run"]
    assert {c[1][0] for c in seed_run_calls} == {"EcalPedestals", "SiStripBad"}
    assert {c[1][0] for c in add_ls_calls} == {"EcalPedestals", "SiStripBad"}
    assert end_run_calls == [("end_run", (500,), {})]  # exactly one, calibration-agnostic


def test_scenario_files_in_repo_parse_and_build_a_nonempty_timeline():
    import yaml

    scenarios_dir = SCENARIO_PLAYER_DIR / "scenarios"
    yaml_files = list(scenarios_dir.glob("*.yaml"))
    assert yaml_files, "expected at least one example scenario under scenario-player/scenarios/"
    saw_a_fault = False
    for path in yaml_files:
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        events = scenario_player.build_timeline(scenario)
        assert events, f"{path} produced an empty timeline"
        # Timeline must already be sorted (build_timeline's own contract).
        assert [e.at for e in events] == sorted(e.at for e in events)
        saw_a_fault = saw_a_fault or any(e.kind == "fault" for e in events)
    assert saw_a_fault, "expected at least one committed scenario to exercise the faults: section"


# --- faults: -------------------------------------------------------------------------


def test_build_timeline_parses_faults_interleaved_with_run_events():
    scenario = {
        "runs": [
            {"calibrations": ["EcalPedestals"], "run": 398600, "start_offset": 0, "end_after": 100},
        ],
        "faults": [
            {"target": "oms", "mode": "timeout", "at": 10},
            {
                "target": "cmsrun",
                "mode": "exit_code",
                "exit_code": 139,
                "calibration": "EcalPedestals",
                "step": "step2",
                "at": 50,
            },
        ],
    }
    events = scenario_player.build_timeline(scenario)
    fault_events = [e for e in events if e.kind == "fault"]
    assert [e.at for e in fault_events] == [10, 50]
    assert fault_events[0].fault.target == "oms" and fault_events[0].fault.mode == "timeout"
    assert fault_events[1].fault.exit_code == 139 and fault_events[1].fault.step == "step2"
    # Interleaved with the run's own events by time, not appended at the end.
    assert [e.at for e in events] == sorted(e.at for e in events)


def test_build_timeline_fault_at_defaults_to_zero():
    events = scenario_player.build_timeline({"faults": [{"target": "oms", "mode": "timeout"}]})
    assert events[0].at == 0


def test_build_timeline_rejects_invalid_fault_entry():
    with pytest.raises(ValueError, match="target"):
        scenario_player.build_timeline({"faults": [{"target": "bogus", "mode": "timeout"}]})


def test_fault_event_describe_is_readable():
    events = scenario_player.build_timeline(
        {
            "faults": [
                {
                    "target": "cmsrun",
                    "mode": "exit_code",
                    "exit_code": 1,
                    "calibration": "BeamSpot",
                    "run": 500,
                    "at": 5,
                }
            ]
        }
    )
    description = events[0].describe()
    assert "arm-fault" in description
    assert "cmsrun" in description
    assert "BeamSpot" in description
    assert "500" in description


def test_dry_run_does_not_arm_faults(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "arm_fault", lambda *a, **kw: calls.append((a, kw)))

    events = scenario_player.build_timeline({"faults": [{"target": "oms", "mode": "timeout", "at": 1}]})
    scenario_player.play(events, dry_run=True, log=lambda _msg: None)

    assert calls == []


def test_play_arms_fault_with_expected_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "arm_fault", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(scenario_player.time, "sleep", lambda _seconds: None)

    events = scenario_player.build_timeline(
        {
            "faults": [
                {
                    "target": "cmsrun",
                    "mode": "exit_code",
                    "exit_code": 139,
                    "calibration": "EcalPedestals",
                    "run": 398600,
                    "step": "step2",
                    "times": 2,
                    "message": "custom crash text",
                    "at": 1,
                }
            ]
        }
    )
    scenario_player.play(events, log=lambda _msg: None)

    assert calls == [
        (
            ("cmsrun", "exit_code"),
            {
                "status": None,
                "exit_code": 139,
                "calibration": "EcalPedestals",
                "run": 398600,
                "step": "step2",
                "times": 2,
                "message": "custom crash text",
            },
        )
    ]
