"""
Tests for scenario-player/scenario_player.py -- the timed, engine-agnostic run/lumisection
playback tool (see scenario-player/scenarios/*.yaml for example scenario files). These
tests never touch $NGT_DEV_HOME or the filesystem: scenario-player/seed.py's actual
seed_run/add_ls/end_run are monkeypatched out so we can assert purely on
event ordering/timing and on what scenario_player would have called.
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_PLAYER_DIR = REPO_ROOT / "scenario-player"
if str(SCENARIO_PLAYER_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIO_PLAYER_DIR))

import fake_conddb  # noqa: E402
import scenario_player  # noqa: E402


def _record_seed_calls(monkeypatch):
    """Replace scenario-player/seed.py's three entry points with recorders; returns the
    list they append to, so a test can assert purely on what would have been called."""
    calls = []
    for name in ("seed_run", "add_ls", "end_run"):
        monkeypatch.setattr(
            scenario_player.seed, name, lambda *a, _name=name, **kw: calls.append((_name, a, kw))
        )
    return calls


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
    # Only one run can be active at a time; concurrent calibrations on the
    # same run go under one run's 'calibrations' list instead (see the next
    # test).
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
    calls = _record_seed_calls(monkeypatch)

    events = scenario_player.build_timeline(
        {"runs": [{"calibrations": ["BeamSpot"], "run": 7, "lumisections": [{"ls": 1, "after": 0}], "end_after": 1}]}
    )
    lines = []
    scenario_player.show_timeline(events, log=lines.append)

    assert calls == []  # dry-run must not touch the fake OMS/EOS state at all
    assert len(lines) == len(events) == 3
    assert "seed-run" in lines[0]
    assert "add-ls" in lines[1]
    assert "end-run" in lines[2]


def test_play_fires_events_in_order_with_expected_arguments(monkeypatch):
    calls = _record_seed_calls(monkeypatch)
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
    calls = _record_seed_calls(monkeypatch)
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


def _three_instant_scenario(calibrations=("BeamSpot",)):
    """One run whose events land at three distinct offsets: +0s (start_run plus LS 1),
    +5s (LS 2), +10s (end_run)."""
    return {
        "runs": [
            {
                "calibrations": list(calibrations),
                "run": 7,
                "lumisections": [{"ls": 1, "after": 0}, {"ls": 2, "after": 5}],
                "end_after": 5,
            }
        ]
    }


def test_interactive_waits_for_confirmation_instead_of_sleeping(monkeypatch):
    calls = _record_seed_calls(monkeypatch)
    sleeps = []
    monkeypatch.setattr(scenario_player.time, "sleep", sleeps.append)
    prompts = []

    events = scenario_player.build_timeline(_three_instant_scenario())
    scenario_player.play(events, log=lambda _msg: None, prompt=prompts.append)

    # The scenario's delays are *replaced* by the confirmations, not waited out as well.
    assert sleeps == []
    assert len(prompts) == 3
    assert [c[0] for c in calls] == ["seed_run", "add_ls", "add_ls", "end_run"]


def test_interactive_asks_once_per_scheduled_instant_not_once_per_calibration(monkeypatch):
    calls = _record_seed_calls(monkeypatch)
    prompts = []

    # Two calibrations on one run: 7 events (2 seed-run, 4 add-ls, 1 end-run) but still
    # only the same 3 offsets, so one keypress each rather than one per event.
    events = scenario_player.build_timeline(_three_instant_scenario(["EcalPedestals", "SiStripBad"]))
    scenario_player.play(events, log=lambda _msg: None, prompt=prompts.append)

    assert len(events) == 7
    assert len(prompts) == 3
    assert "seed-run --calibration EcalPedestals --run 7" in prompts[0]
    assert "seed-run --calibration SiStripBad --run 7" in prompts[0]
    assert len(calls) == 7


def test_interactive_prompt_names_the_next_events_and_counts_the_steps(monkeypatch):
    _record_seed_calls(monkeypatch)
    prompts = []

    events = scenario_player.build_timeline(_three_instant_scenario())
    scenario_player.play(events, log=lambda _msg: None, prompt=prompts.append)

    assert prompts[0].startswith("[1/3] next, scheduled +0.0s -- 2 event(s):")
    assert "seed-run --calibration BeamSpot --run 7" in prompts[0]
    assert "add-ls --calibration BeamSpot --run 7 --ls 1" in prompts[0]
    assert prompts[2].startswith("[3/3] next, scheduled +10.0s -- 1 event(s):")
    assert "end-run --run 7" in prompts[2]
    # The prompt is the last line, so whatever is asked is what the terminal shows last.
    assert prompts[2].endswith(">>> Press Enter to fire (Ctrl-C to stop here)... ")


def test_interactive_fires_nothing_before_its_own_confirmation(monkeypatch):
    calls = _record_seed_calls(monkeypatch)
    fired_so_far = []

    events = scenario_player.build_timeline(_three_instant_scenario())
    scenario_player.play(
        events, log=lambda _msg: None, prompt=lambda _text: fired_so_far.append(len(calls))
    )

    # Instants of 2, 1 and 1 events: nothing may be fired ahead of its own keypress.
    assert fired_so_far == [0, 2, 3]
    assert len(calls) == 4


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_interactive_lets_prompt_failures_reach_the_caller(monkeypatch, failure):
    calls = _record_seed_calls(monkeypatch)
    asked = []

    def prompt(text):
        asked.append(text)
        if len(asked) == 2:
            raise failure()

    events = scenario_player.build_timeline(_three_instant_scenario())
    # play() holds no policy on either of these -- main() decides what a Ctrl-C or a
    # closed stdin means, and whether it's worth an exit code.
    with pytest.raises(failure):
        scenario_player.play(events, log=lambda _msg: None, prompt=prompt)

    assert [c[0] for c in calls] == ["seed_run", "add_ls"]  # only the first instant's events


def test_timed_playback_sleeps_and_never_prompts(monkeypatch):
    calls = _record_seed_calls(monkeypatch)
    sleeps = []
    monkeypatch.setattr(scenario_player.time, "sleep", sleeps.append)

    events = scenario_player.build_timeline(_three_instant_scenario())
    scenario_player.play(events, log=lambda _msg: None)

    # Three instants, the first of them at +0.0s -- so only the latter two actually wait.
    assert len(sleeps) == 2
    assert len(calls) == 4


@pytest.mark.parametrize(
    "flags, expected_error",
    [
        (["--interactive", "--dry-run"], "mutually exclusive"),
        (["--interactive", "--speed", "5"], "--speed has no meaning"),
        (["--speed", "0"], "must be positive"),
        (["--monitor", "--dry-run"], "--monitor has no meaning"),
        (["--validate-only", "--no-validate"], "mutually exclusive"),
        (["--validate-only", "--interactive"], "--validate-only plays nothing"),
        (["--validate-only", "--speed", "2"], "--validate-only plays nothing"),
    ],
)
def test_main_rejects_contradictory_flag_combinations(monkeypatch, capsys, flags, expected_error):
    # The scenario path deliberately doesn't exist: these combinations have to be rejected
    # before the file is ever read, so the error is about the flags and nothing else.
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", "does-not-exist.yaml", *flags])

    with pytest.raises(SystemExit) as exit_info:
        scenario_player.main()

    assert exit_info.value.code == 2
    assert expected_error in capsys.readouterr().err


def _write_scenario(tmp_path, scenario):
    """A scenario file on disk -- main() takes a path, not a parsed dict."""
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    return path


def test_only_list_calibrations_prints_the_sorted_union_one_per_line(monkeypatch, capsys, tmp_path):
    calls = _record_seed_calls(monkeypatch)
    scenario = _write_scenario(
        tmp_path,
        {
            "runs": [
                {"calibrations": ["BeamSpot", "EcalPedestals"], "run": 1, "end_after": 10},
                {"calibrations": ["EcalPedestals"], "run": 2, "start_offset": 20, "end_after": 5},
            ]
        },
    )
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", str(scenario), "--only-list-calibrations"])

    assert scenario_player.main() is None

    # Sorted, deduplicated across runs, one name per line and nothing else on stdout:
    # live_demo.sh loops over this output.
    assert capsys.readouterr().out.splitlines() == ["BeamSpot", "EcalPedestals"]
    assert calls == []  # listing is not playing -- it must never fire an event


def test_only_list_calibrations_prints_nothing_for_a_scenario_with_no_runs(monkeypatch, capsys, tmp_path):
    scenario = _write_scenario(tmp_path, {"runs": []})
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", str(scenario), "--only-list-calibrations"])

    assert scenario_player.main() is None
    # Not even the "(scenario has no runs -- nothing to play)" message that plain
    # playback prints: a caller looping over this output would take that line for a
    # calibration name.
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "ignored_flags",
    [
        ["--interactive", "--dry-run"],
        ["--interactive", "--speed", "5"],
        ["--monitor", "--dry-run"],
        ["--speed", "0"],
        ["--validate-only", "--no-validate"],
    ],
)
def test_only_list_calibrations_ignores_every_other_flag(monkeypatch, capsys, tmp_path, ignored_flags):
    # Each of these is a usage error when playing (see the test above) -- alongside
    # --only-list-calibrations they are simply ignored, which is what lets live_demo.sh
    # append the flag to a command line it already has instead of working out which
    # argument is the scenario path. Also pins that the flag is handled *above* main()'s
    # validation block: move it below and every case here raises SystemExit.
    calls = _record_seed_calls(monkeypatch)
    scenario = _write_scenario(
        tmp_path, {"runs": [{"calibrations": ["SiStripBad"], "run": 1, "end_after": 1}]}
    )
    monkeypatch.setattr(
        sys, "argv", ["scenario_player.py", str(scenario), *ignored_flags, "--only-list-calibrations"]
    )

    assert scenario_player.main() is None
    assert capsys.readouterr().out.splitlines() == ["SiStripBad"]
    assert calls == []


@pytest.mark.parametrize("extra_flags", [[], ["--only-list-calibrations"]])
def test_main_reports_a_malformed_scenario_with_exit_2(monkeypatch, capsys, tmp_path, extra_flags):
    # Overlapping runs: one line on stderr, no traceback -- also before live_demo.sh starts loops.
    calls = _record_seed_calls(monkeypatch)
    scenario = _write_scenario(
        tmp_path,
        {
            "runs": [
                {"calibrations": ["SiStripBad"], "run": 1, "start_offset": 0, "end_after": 100},
                {"calibrations": ["SiStripBad"], "run": 2, "start_offset": 50, "end_after": 100},
            ]
        },
    )
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", str(scenario), *extra_flags])

    assert scenario_player.main() == scenario_player.EXIT_BAD_SCENARIO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(scenario) in captured.err and "Traceback" not in captured.err
    assert calls == []


@pytest.mark.skipif(os.name != "posix", reason="the ledger writer uses fcntl.flock")
def test_main_checks_only_the_uploads_made_during_this_play(monkeypatch, tmp_path):
    monkeypatch.setenv("NGT_DEV_HOME", str(tmp_path))
    monkeypatch.delenv("NGT_CONDDB_DIR", raising=False)
    fake_conddb.append_payload({"calibration": "EcalPedestals", "run": 1, "lumisections": [1]})  # an earlier play's
    _record_seed_calls(monkeypatch)
    monkeypatch.setattr(scenario_player.time, "sleep", lambda seconds: None)
    scenario = _write_scenario(
        tmp_path,
        {
            "runs": [{"calibrations": ["EcalPedestals"], "run": 1, "lumisections": [{"ls": 1}]}],
            "expect": {"quiet_for": 0.1, "payloads": []},
        },
    )
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", str(scenario)])

    assert scenario_player.main() == scenario_player.EXIT_OK


def _monitor(tmp_path, **kwargs):
    """A DataDirMonitor over a scratch $NGT_DEV_HOME/data, plus the list it logs into.
    Constructing it baselines whatever the test created beforehand."""
    lines = []
    monitor = scenario_player.DataDirMonitor(tmp_path / "data", log=lines.append, **kwargs)
    return monitor, lines


def test_monitor_reports_only_what_appears_after_it_is_constructed(tmp_path):
    run_dir = tmp_path / "data" / "EcalPedestals" / "run398600"
    run_dir.mkdir(parents=True)
    (run_dir / "runStart.log").write_text("started", encoding="utf-8")

    monitor, lines = _monitor(tmp_path)
    assert monitor.poll() == []  # a demo-env left over from an earlier scenario isn't news

    (run_dir / "runEnd.log").write_text("ended", encoding="utf-8")
    assert monitor.poll() == ["data/EcalPedestals/run398600/runEnd.log  (5 B)"]
    assert len(lines) == 1
    assert "runStart.log" not in lines[0]


def test_monitor_marks_directories_and_lists_parents_first(tmp_path):
    monitor, lines = _monitor(tmp_path)
    alca_dir = tmp_path / "data" / "EcalPedestals" / "run398600" / "alcaPromptJob000"
    alca_dir.mkdir(parents=True)
    (alca_dir / "stdout.log").write_text("job output\n", encoding="utf-8")

    # Directories get a trailing slash and no size; the watched root itself is never
    # reported, only what turns up inside it.
    assert monitor.poll() == [
        "data/EcalPedestals/",
        "data/EcalPedestals/run398600/",
        "data/EcalPedestals/run398600/alcaPromptJob000/",
        "data/EcalPedestals/run398600/alcaPromptJob000/stdout.log  (11 B)",
    ]
    assert "4 new path(s) under $NGT_DEV_HOME:" in lines[0]
    # A report starts on a line of its own, so it can't land on the tail of an
    # --interactive prompt (which is deliberately left unterminated until Enter).
    assert lines[0].startswith("\n  [")


def test_monitor_survives_a_data_directory_that_does_not_exist_yet(tmp_path):
    monitor, lines = _monitor(tmp_path)

    assert monitor.poll() == []
    assert lines == []
    # The player must never create environment state: a missing data directory means
    # `live_demo.sh setup` hasn't run, and silently making one would hide that.
    assert not monitor.root.exists()

    (monitor.root / "SiStripBad").mkdir(parents=True)
    assert monitor.poll() == ["data/SiStripBad/"]


def test_monitor_reports_each_path_once(tmp_path):
    monitor, _lines = _monitor(tmp_path)
    run_dir = tmp_path / "data" / "EcalPedestals" / "run398600"
    run_dir.mkdir(parents=True)
    script = run_dir / "ALCAOUTPUT.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")

    assert any("ALCAOUTPUT.sh" in line for line in monitor.poll())
    assert monitor.poll() == []  # not again for merely still existing

    # ALCAOUTPUT.sh self-deletes on success -- going away is silent, but a regenerated
    # one is news again.
    script.unlink()
    assert monitor.poll() == []
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    assert monitor.poll() == ["data/EcalPedestals/run398600/ALCAOUTPUT.sh  (12 B)"]


def test_monitor_stop_reports_what_appeared_during_the_last_interval(tmp_path):
    # interval=60 keeps this deterministic: the watcher thread parks in wait(60) for the
    # whole test, so only stop()'s own final poll can possibly report the new directory.
    monitor, lines = _monitor(tmp_path, interval=60)
    monitor.start()
    (monitor.root / "BeamSpot").mkdir(parents=True)
    monitor.stop()

    assert len(lines) == 1
    assert "data/BeamSpot/" in lines[0]

    monitor.stop()  # idempotent, and there's nothing new left to report
    assert len(lines) == 1


def test_scenario_files_in_repo_parse_and_build_a_nonempty_timeline():
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

CMSRUN_FAULT = {"target": "cmsrun", "mode": "exit_code", "exit_code": 139, "calibration": "EcalPedestals",
                "run": 398600, "step": "step2"}


def test_build_timeline_interleaves_faults_with_run_events():
    scenario = {
        "runs": [{"calibrations": ["EcalPedestals"], "run": 398600, "start_offset": 0, "end_after": 100}],
        "faults": [{"target": "oms", "mode": "timeout"}, {**CMSRUN_FAULT, "at": 50}],
    }
    events = scenario_player.build_timeline(scenario)

    assert [(e.at, e.kind) for e in events] == [(0, "start_run"), (0, "fault"), (50, "fault"), (100, "end_run")]
    assert events[1].fault.mode == "timeout"  # `at` defaults to 0
    assert events[2].fault == scenario_player.ngt_faults.parse_fault_spec(CMSRUN_FAULT)
    assert events[2].describe() == (
        "arm-fault --target cmsrun --mode exit_code (calibration=EcalPedestals, run=398600, step=step2)"
    )


def test_build_timeline_rejects_invalid_fault_entry():
    with pytest.raises(ValueError, match="target"):
        scenario_player.build_timeline({"faults": [{"target": "bogus", "mode": "timeout"}]})


def test_only_list_calibrations_skips_fault_events(monkeypatch, capsys, tmp_path):
    # An oms fault has no calibration at all (sorting None alongside names would raise), and a
    # scoped cmsrun fault names a calibration whose loops only its runs could need.
    scenario = _write_scenario(
        tmp_path,
        {
            "runs": [{"calibrations": ["EcalPedestals"], "run": 1, "end_after": 10}],
            "faults": [{"target": "oms", "mode": "timeout", "at": 1}, {**CMSRUN_FAULT, "calibration": "BeamSpot"}],
        },
    )
    monkeypatch.setattr(sys, "argv", ["scenario_player.py", str(scenario), "--only-list-calibrations"])

    assert scenario_player.main() is None
    assert capsys.readouterr().out.splitlines() == ["EcalPedestals"]


def test_play_arms_the_parsed_fault(monkeypatch):
    calls = []
    monkeypatch.setattr(scenario_player.seed, "arm_fault", calls.append)
    monkeypatch.setattr(scenario_player.time, "sleep", lambda _seconds: None)

    events = scenario_player.build_timeline({"faults": [{**CMSRUN_FAULT, "times": 2, "at": 1}]})
    scenario_player.play(events, log=lambda _msg: None)

    assert calls == [events[0].fault]
    assert calls[0].times == 2
