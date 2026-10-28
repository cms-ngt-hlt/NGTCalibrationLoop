#!/usr/bin/env python3
"""
(FAKE) stand-in for CMS conditions upload's `uploadConditions.py`.

Never touches the network or a real conditions DB -- it records the upload in a dummy one,
$NGT_DEV_HOME/conddb/payloads.jsonl (scenario-player/fake_conddb.py), with the calibration/run/
lumisections the .db's own provenance manifest names. The metadata .txt Step 4 writes beside the
.db is not read: it is the production code's own output, not evidence of what was processed.

A fault armed for this job (faults.consume_armed: $NGT_FAULTS_DIR/upload_conditions.json, scoped by the
working directory) makes it print faults.describe()'s line and exit with the fault's code instead.
"""
import os
import sys

# env.sh puts this checkout's scenario-player/bin on $PATH, so scenario-player/faults.py and
# fake_conddb.py are always one directory up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import fake_conddb  # noqa: E402
import faults  # noqa: E402


def main():
    fault = faults.consume_armed("upload_conditions")
    if fault:
        print(faults.describe("upload_conditions", fault), file=sys.stderr)
        sys.exit(fault["exit_code"])

    db_file = sys.argv[1] if len(sys.argv) > 1 else "<missing db file argument>"
    cond_auth_path = os.environ.get("COND_AUTH_PATH", "<unset>")

    # HARVESTING.sh still calls this after its cmsRun failed and left no .db (Step 4 runs it with
    # `bash`, so its shebang's -e doesn't apply): fail like a real upload, record nothing.
    manifest = fake_conddb.read_provenance(db_file)
    if manifest is None:
        reason = "no provenance manifest in it" if os.path.exists(db_file) else "no such file"
        print(f"(FAKE) uploadConditions.py: ERROR - cannot read {db_file}: {reason}", file=sys.stderr)
        sys.exit(1)

    row = fake_conddb.append_payload(
        {
            "calibration": manifest["calibration"],
            "run": manifest["run"],
            "lumisections": manifest["lumisections"],
            "n_input_files": len(manifest["inputs"]),
            "db_file": os.path.basename(db_file),
            "job_dir": os.getcwd(),
        }
    )

    print(f"(FAKE) uploadConditions.py: would upload {db_file} using COND_AUTH_PATH={cond_auth_path}")
    print(
        f"(FAKE) uploadConditions.py: recorded payload seq={row['seq']} {row['calibration']}/run{row['run']} "
        f"covering LS {','.join(map(str, row['lumisections'])) or 'none'} -> {fake_conddb.ledger_path()}"
    )


if __name__ == "__main__":
    main()
