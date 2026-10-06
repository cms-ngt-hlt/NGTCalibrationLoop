"""
(FAKE) provenance manifests: what the fake toolchain writes into its outputs. Standard library
only -- the fakes in bin/ import it from the directory above, like faults.py.

A provenance manifest is what a fake cmsRun output contains: a magic first line, then one JSON
object naming the calibration/run/lumisections that went into it -- the union of its inputs'
manifests or, for a raw file seed.py wrote, the LS in its run<RUN>_ls<LS> name. So identifiers
travel the real data path, step2 -> step3 -> step4.
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone

PROVENANCE_MAGIC = "(FAKE-NGT-PROVENANCE-v1)"
# How seed.py names the raw files (bin/edmFileUtil parses the same): run<RUN>_ls<LS>....root.
RAW_LS_RE = re.compile(r"run(\d+)_ls(\d+)")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write_provenance(path, *, lumisections, inputs, step=None, calibration=None, run=None):
    """Write one output file as a provenance manifest. Returns the manifest."""
    manifest = {
        "step": step,
        "calibration": calibration,
        "run": _as_int(run),
        "lumisections": sorted(lumisections),
        "inputs": sorted(os.path.basename(str(p)) for p in inputs),
        "produced_at": utc_now(),
    }
    # Atomically, like faults.atomic_write_json: a reader sees the old manifest or the new one.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-prov-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(PROVENANCE_MAGIC + "\n")
            json.dump(manifest, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return manifest


def read_provenance(path):
    """The manifest in `path`, or None if it holds none (a raw seeded file) or can't be read."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.readline().strip() != PROVENANCE_MAGIC:
                return None
            return json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def lumisections_of_input(path):
    """The LS one cmsRun input contributes: its manifest's, else the one in a raw file's name.
    An unrecognised input contributes none, as bin/edmFileUtil reports "ERR" for it."""
    manifest = read_provenance(path)
    if manifest is not None:
        return set(manifest.get("lumisections", []))
    match = RAW_LS_RE.search(os.path.basename(str(path)))
    return {int(match.group(2))} if match else set()


def merge_input_lumisections(paths):
    """Union of every input's lumisections -- what a step's output covers."""
    return set().union(*(lumisections_of_input(path) for path in paths))
