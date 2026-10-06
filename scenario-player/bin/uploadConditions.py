#!/usr/bin/env python3
"""
(FAKE) stand-in for CMS conditions upload's `uploadConditions.py`.

Never touches the network or a real conditions DB -- just reports what it
would have uploaded, so NGTLoopStep4.py's harvesting script can complete its
cycle.

A fault armed for this job (faults.consume_armed: $NGT_FAULTS_DIR/upload_conditions.json, scoped by the
working directory) makes it print faults.describe()'s line and exit with the fault's code instead.
"""
import os
import sys

# env.sh puts this checkout's scenario-player/bin on $PATH, so scenario-player/faults.py is always
# one directory up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import faults  # noqa: E402


def main():
    fault = faults.consume_armed("upload_conditions")
    if fault:
        print(faults.describe("upload_conditions", fault), file=sys.stderr)
        sys.exit(fault["exit_code"])

    db_file = sys.argv[1] if len(sys.argv) > 1 else "<missing db file argument>"
    cond_auth_path = os.environ.get("COND_AUTH_PATH", "<unset>")
    print(f"(FAKE) uploadConditions.py: would upload {db_file} using COND_AUTH_PATH={cond_auth_path}")


if __name__ == "__main__":
    main()
