#!/bin/bash
# Engine-agnostic pieces of the live-demo/live-test setup: the scratch
# environment location, and how to (re)create the fake OMS/EOS/CMSSW
# toolchain + calibration config inside it.
#
# This file knows nothing about Airflow (or any other workflow engine) --
# it's the "simulate a live calibration run" half of the live-test setup,
# deliberately kept separate from the "drive a specific orchestration engine"
# half (airflow_automation/airflow_demo/airflow_env.sh / airflow_automation/airflow_demo/airflow_demo.sh today). An adapter for a
# different engine (Prefect, Kestra, REANA, ...) would source this same file
# and reuse `sim_setup` unchanged -- only the engine-specific script differs.
#
# The other engine-agnostic piece of the live-test setup is scenario-player/seed.py (one-
# shot seed-run/add-ls/end-run/list-runs commands, also importable as a
# library) and scenario-player/scenario_player.py, which plays back a whole timed
# sequence of runs/lumisections (scenario-player/scenarios/*.yaml) instead of single
# commands -- point either at $NGT_DEV_HOME while any engine adapter is
# running and it should react the same way, since neither one imports or
# knows about a specific engine.
#
# Usage:
#   source scenario-player/sim_env.sh          -- sets REPO_DIR, NGT_DEV_HOME, and the
#                                      sim_setup/sim_reset functions; doesn't
#                                      execute anything on its own.
#   ./scenario-player/sim_env.sh setup|reset   -- run directly instead. `reset` wipes
#                                      $NGT_DEV_HOME (all simulator state --
#                                      fake OMS runs, EOS files, output data,
#                                      logs) independently of whatever's using
#                                      it, no venv needed. `setup` recreates
#                                      it, and DOES need a venv active first
#                                      (any one -- .venv, ~/airflow3-ngt-venv,
#                                      whichever) for its `pip install -e
#                                      tests/stubs` step; run directly with
#                                      none active and it fails fast with a
#                                      clear message instead of pip's cryptic
#                                      "externally-managed-environment" error.
#                                      Neither touches any engine's OWN state
#                                      (e.g. Airflow's DAG-run history) -- see
#                                      airflow_automation/airflow_demo/airflow_demo.sh's reset-* commands.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

# sim_setup: create/refresh everything under $NGT_DEV_HOME that any engine
# adapter needs -- the fake scenario-player/bin/ toolchain on $PATH, ngtParameters.jsn,
# and (via scenario-player/seed.py, itself fully engine-agnostic) the per-calibration
# fake EOS layout and an empty fake OMS runs file. Safe to re-run.
#
# Needs an active venv (any one) for its `pip install -e tests/stubs` step
# below -- checked upfront so a forgotten `source .../activate` fails with a
# clear message here instead of pip's much less obvious
# "externally-managed-environment" error (Debian/Ubuntu's system Python
# refuses `pip install` outright, per PEP 668).
sim_setup() {
  if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "sim_setup needs an active Python venv (for 'pip install -e tests/stubs')." >&2
    echo "Activate one first, e.g.:" >&2
    echo "  source ~/airflow3-ngt-venv/bin/activate   # driving the Airflow adapter" >&2
    echo "  source .venv/bin/activate                 # or the plain venv used for pytest" >&2
    return 1
  fi

  mkdir -p "$NGT_DEV_HOME"/{data,logs,cond_auth,calibrationYAML,eos,bin}
  mkdir -p "$NGT_DEV_HOME/cmssw_home/CMSSW_16_0_7_patch1/src"

  cp "$REPO_DIR"/scenario-player/bin/* "$NGT_DEV_HOME/bin/"
  chmod +x "$NGT_DEV_HOME"/bin/*

  cat > "$NGT_DEV_HOME/ngtParameters.jsn" <<JSON
{
    "SCRAM_ARCH": "el8_amd64_gcc13",
    "CMSSW_VERSION": "CMSSW_16_0_7_patch1",
    "GLOBAL_TAG": "160X_dataRun3_ExpressNGT_v0",
    "DATA_BASE_PATH": "$NGT_DEV_HOME/data",
    "LOG_BASE_PATH": "$NGT_DEV_HOME/logs",
    "COND_AUTH_PATH": "$NGT_DEV_HOME/cond_auth"
}
JSON

  python3 "$REPO_DIR/scenario-player/seed.py" setup

  # Makes the fake `omsapi` package importable by whatever Python process is
  # actually running the engine's workers (see airflow_automation/airflow_demo/airflow_demo.sh's note on
  # why this is a real pip install rather than a PYTHONPATH export). Harmless
  # no-op for an engine that doesn't need it.
  pip install -e "$REPO_DIR/tests/stubs" -q
}

# sim_reset: wipe all simulator state -- every fake run/lumisection/EOS file/
# output $NGT_DEV_HOME holds, regardless of which engine (if any) produced or
# is watching it. Does NOT recreate it afterward (call sim_setup for that) and
# does NOT touch any engine's own state -- an engine that keeps run-history
# outside $NGT_DEV_HOME (e.g. Airflow's metadata DB) needs its own reset too,
# see airflow_automation/airflow_demo/airflow_demo.sh's reset-* commands.
sim_reset() {
  echo "Removing $NGT_DEV_HOME"
  rm -rf "$NGT_DEV_HOME"
}

# Allow running this file directly for setup/reset (neither needs any
# engine-specific environment) as well as sourcing it for sim_setup/
# NGT_DEV_HOME/REPO_DIR (still the primary, documented usage -- see above).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  case "${1:-}" in
    setup) sim_setup; echo "Set up $NGT_DEV_HOME" ;;
    reset) sim_reset ;;
    *) echo "Usage: $0 <setup|reset>" >&2; exit 1 ;;
  esac
fi
