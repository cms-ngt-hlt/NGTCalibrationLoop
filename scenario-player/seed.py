#!/usr/bin/env python3
"""
Helper for scenario-player/live_demo.sh: patches calibrationYAML into the scratch demo
environment, and seeds/updates the fake OMS run data + fake "EOS" RAW files
that drive a live NGTLoopStep2.py process.

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


def cmd_seed_run(args):
    runs = [r for r in _load_runs() if r["run_number"] != args.run]
    start_time = datetime.now(timezone.utc) - timedelta(minutes=args.minutes_ago)
    runs.append(
        {
            "run_number": args.run,
            "fill_type_runtime": "PROTONS",
            "l1_hlt_mode": "collisions2026",
            "stable_beam": True,
            "start_time": _iso(start_time),
            "end_time": None,
            "last_lumisection_number": max(args.ls) if args.ls else 0,
        }
    )
    _save_runs(runs)

    eos_dir = _run_eos_dir(args.calibration, args.run)
    eos_dir.mkdir(parents=True, exist_ok=True)
    for ls in args.ls:
        _touch_ls_file(eos_dir, args.run, ls)

    print(f"Seeded live run {args.run} for {args.calibration} (started {start_time.isoformat()})")
    print(f"EOS dir: {eos_dir}" + (f" ({len(args.ls)} LS file(s) seeded)" if args.ls else " (no LS files yet)"))


def cmd_add_ls(args):
    runs = _load_runs()
    if not any(r["run_number"] == args.run for r in runs):
        print(f"No such run {args.run} in {OMS_RUNS_FILE} -- run seed-run first", file=sys.stderr)
        sys.exit(1)
    for r in runs:
        if r["run_number"] == args.run:
            r["last_lumisection_number"] = max(r.get("last_lumisection_number", 0), args.ls)
    _save_runs(runs)

    eos_dir = _run_eos_dir(args.calibration, args.run)
    eos_dir.mkdir(parents=True, exist_ok=True)
    path = _touch_ls_file(eos_dir, args.run, args.ls)
    print(f"Added LS {args.ls} for run {args.run}: {path}")


def cmd_end_run(args):
    runs = _load_runs()
    if not any(r["run_number"] == args.run for r in runs):
        print(f"No such run {args.run} in {OMS_RUNS_FILE}", file=sys.stderr)
        sys.exit(1)
    for r in runs:
        if r["run_number"] == args.run:
            r["end_time"] = _iso(datetime.now(timezone.utc))
    _save_runs(runs)
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
