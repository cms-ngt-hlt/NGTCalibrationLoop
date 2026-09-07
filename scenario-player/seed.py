#!/usr/bin/env python3
"""
Helper for the engine adapters (airflow_automation/airflow_demo/airflow_demo.sh etc.): patches
calibrationYAML into the scratch demo environment, and seeds/updates the fake
OMS run data + fake "EOS" RAW files that drive a live calibration workflow.

The one-shot CLI verbs below (seed-run/add-ls/end-run/list-runs) are also
exposed as plain importable functions (seed_run/add_ls/end_run) -- that's what
scenario-player/scenario_player.py calls directly to play back a timed sequence of runs/
lumisections instead of firing single commands by hand. Both ways of driving
this file are engine-agnostic: neither knows or cares whether Airflow (or
anything else) is watching $NGT_DEV_HOME.

All state lives under $NGT_DEV_HOME (default ./demo-env, a gitignored directory
inside this repo) -- delete it any time to reset the demo.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_DIR = Path(__file__).resolve().parent.parent
NGT_DEV_HOME = Path(os.environ.get("NGT_DEV_HOME", str(REPO_DIR / "demo-env")))
OMS_RUNS_FILE = NGT_DEV_HOME / "oms_runs.json"
CALIB_YAML_DIR = NGT_DEV_HOME / "calibrationYAML"
CALIBRATIONS = ("SiStripBad", "EcalPedestals", "BeamSpot")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_str(run_number):
    s = str(run_number)
    return f"{s[:3]}/{s[3:]}" if len(s) == 6 else s


def _load_runs():
    if OMS_RUNS_FILE.exists():
        return json.loads(OMS_RUNS_FILE.read_text(encoding="utf-8"))
    return []


def _save_runs(runs):
    OMS_RUNS_FILE.write_text(json.dumps(runs, indent=2), encoding="utf-8")


def _eos_dir(calibration):
    calib_yaml = yaml.safe_load((CALIB_YAML_DIR / f"{calibration}.yaml").read_text(encoding="utf-8"))
    return Path(calib_yaml["file_in_path"])


def _run_eos_dir(calibration, run_number):
    return _eos_dir(calibration) / _run_str(run_number) / "00000"


def _touch_ls_file(eos_dir, run_number, ls_number):
    path = eos_dir / f"run{run_number}_ls{ls_number:04d}.root"
    path.write_bytes(b"(FAKE) raw data placeholder -- see scenario-player/bin/edmFileUtil\n")
    return path


def cmd_setup(_args):
    CALIB_YAML_DIR.mkdir(parents=True, exist_ok=True)
    for calib in CALIBRATIONS:
        src = REPO_DIR / "calibrationYAML" / f"{calib}.yaml"
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        data["file_in_path"] = str(NGT_DEV_HOME / "eos" / calib) + "/"
        data["step_4_config"]["cmssw_base_path"] = str(NGT_DEV_HOME / "cmssw_home") + "/"
        (CALIB_YAML_DIR / f"{calib}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        (NGT_DEV_HOME / "eos" / calib).mkdir(parents=True, exist_ok=True)
        print(f"Patched {calib}.yaml -> file_in_path={data['file_in_path']}")

    if not OMS_RUNS_FILE.exists():
        _save_runs([])
        print(f"Initialized {OMS_RUNS_FILE}")


def seed_run(calibration, run, minutes_ago=2, ls=None):
    """Latch a new live run onto the fake OMS, optionally with LS files already
    present. Library form of the `seed-run` CLI verb -- see cmd_seed_run."""
    ls = ls or []
    runs = [r for r in _load_runs() if r["run_number"] != run]
    start_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    runs.append(
        {
            "run_number": run,
            "fill_type_runtime": "PROTONS",
            "l1_hlt_mode": "collisions2026",
            "stable_beam": True,
            "start_time": _iso(start_time),
            "end_time": None,
            "last_lumisection_number": max(ls) if ls else 0,
        }
    )
    _save_runs(runs)

    eos_dir = _run_eos_dir(calibration, run)
    eos_dir.mkdir(parents=True, exist_ok=True)
    for one_ls in ls:
        _touch_ls_file(eos_dir, run, one_ls)
    return eos_dir


def add_ls(calibration, run, ls):
    """Drop one more fake RAW LS file for an already-seeded run. Library form
    of the `add-ls` CLI verb -- see cmd_add_ls. Raises ValueError if `run`
    hasn't been seeded yet."""
    runs = _load_runs()
    if not any(r["run_number"] == run for r in runs):
        raise ValueError(f"No such run {run} in {OMS_RUNS_FILE} -- run seed-run first")
    for r in runs:
        if r["run_number"] == run:
            r["last_lumisection_number"] = max(r.get("last_lumisection_number", 0), ls)
    _save_runs(runs)

    eos_dir = _run_eos_dir(calibration, run)
    eos_dir.mkdir(parents=True, exist_ok=True)
    return _touch_ls_file(eos_dir, run, ls)


def end_run(run):
    """Mark a run as ended in the fake OMS (sets end_time to now). Library
    form of the `end-run` CLI verb -- see cmd_end_run. Raises ValueError if
    `run` hasn't been seeded yet."""
    runs = _load_runs()
    if not any(r["run_number"] == run for r in runs):
        raise ValueError(f"No such run {run} in {OMS_RUNS_FILE}")
    for r in runs:
        if r["run_number"] == run:
            r["end_time"] = _iso(datetime.now(timezone.utc))
    _save_runs(runs)


def cmd_seed_run(args):
    eos_dir = seed_run(args.calibration, args.run, minutes_ago=args.minutes_ago, ls=args.ls)
    print(f"Seeded live run {args.run} for {args.calibration}")
    print(f"EOS dir: {eos_dir}" + (f" ({len(args.ls)} LS file(s) seeded)" if args.ls else " (no LS files yet)"))


def cmd_add_ls(args):
    try:
        path = add_ls(args.calibration, args.run, args.ls)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print(f"Added LS {args.ls} for run {args.run}: {path}")


def cmd_end_run(args):
    try:
        end_run(args.run)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print(f"Ended run {args.run} (Step 2 will finalize it once file-availability catches up)")


def cmd_list_runs(_args):
    runs = _load_runs()
    if not runs:
        print(f"(no runs seeded in {OMS_RUNS_FILE})")
    for r in runs:
        print(json.dumps(r))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Patch calibrationYAML into the scratch demo environment").set_defaults(
        func=cmd_setup
    )

    p_seed = sub.add_parser("seed-run", help="Latch a new live run onto the fake OMS, optionally with LS files")
    p_seed.add_argument("--calibration", required=True, choices=CALIBRATIONS)
    p_seed.add_argument("--run", required=True, type=int)
    p_seed.add_argument("--minutes-ago", type=int, default=2, help="run start time, minutes in the past")
    p_seed.add_argument("--ls", type=int, nargs="*", default=[], help="lumisection numbers to seed immediately")
    p_seed.set_defaults(func=cmd_seed_run)

    p_add = sub.add_parser("add-ls", help="Drop one more fake RAW LS file for an already-seeded run")
    p_add.add_argument("--calibration", required=True, choices=CALIBRATIONS)
    p_add.add_argument("--run", required=True, type=int)
    p_add.add_argument("--ls", required=True, type=int)
    p_add.set_defaults(func=cmd_add_ls)

    p_end = sub.add_parser("end-run", help="Mark a run as ended in the fake OMS (sets end_time to now)")
    p_end.add_argument("--run", required=True, type=int)
    p_end.set_defaults(func=cmd_end_run)

    sub.add_parser("list-runs", help="Print all runs currently seeded in the fake OMS").set_defaults(
        func=cmd_list_runs
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
