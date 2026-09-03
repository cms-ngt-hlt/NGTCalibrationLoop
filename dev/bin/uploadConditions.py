#!/usr/bin/env python3
"""
(FAKE) stand-in for CMS conditions upload's `uploadConditions.py`.

Never touches the network or a real conditions DB -- just reports what it would
have uploaded, so NGTLoopStep4.py's harvesting script can complete its cycle.
"""
import os
import sys


def main():
    db_file = sys.argv[1] if len(sys.argv) > 1 else "<missing db file argument>"
    cond_auth_path = os.environ.get("COND_AUTH_PATH", "<unset>")
    print(f"(FAKE) uploadConditions.py: would upload {db_file} using COND_AUTH_PATH={cond_auth_path}")


if __name__ == "__main__":
    main()
