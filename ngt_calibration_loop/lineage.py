"""What each job's output was made from: `<output>.lineage.json` beside it, with the lumisections it holds and its
inputs. So which lumisections went into a payload is known wherever the steps ran, real CMSSW included, whose outputs
don't say.

Each step writes its output's lineage once its job has succeeded, from what it already knows: Step 2 from the
lumisections edmFileUtil reported for its input files, Steps 3 and 4 from the union of their inputs' own lineage, read
before the job runs (Step 3's job deletes its inputs). An output whose inputs aren't all known gets none: a partial
answer would be a wrong one.

    {"lumisections": [1246, 1247, ...], "inputs": ["/path/of/an/input.root", ...]}
"""

import json
from pathlib import Path

SUFFIX = ".lineage.json"


def path_of(output):
    return Path(f"{output}{SUFFIX}")


def write(output, lumisections, inputs):
    """Record `output`'s lineage, atomically: a reader sees all of it or nothing. Returns the record."""
    record = {"lumisections": sorted(set(lumisections)), "inputs": sorted(str(p) for p in inputs)}
    path = path_of(output)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(record, indent=2), encoding="utf-8")
    partial.replace(path)
    return record


def read(output):
    """`output`'s lineage, or None if it has none."""
    try:
        return json.loads(path_of(output).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lumisections_of(outputs):
    """The lumisections `outputs` hold between them, or None if one of them has no lineage."""
    union = set()
    for output in outputs:
        record = read(output)
        if record is None:
            return None
        union.update(record["lumisections"])
    return union
