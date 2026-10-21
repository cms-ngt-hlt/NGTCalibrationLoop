#!/bin/bash
# Engine-agnostic pieces of the live-demo/live-test setup: the scratch
# environment location, and how to (re)create the fake OMS/EOS/CMSSW
# toolchain + calibration config inside it.
#
# This file knows nothing about any specific workflow engine -- it's the
# "simulate a live calibration run" half of the live-test setup, deliberately
# kept separate from whatever drives a specific engine. An adapter for any
# engine would source this same file and reuse `sim_setup` unchanged -- only
# the engine-specific script differs.
#
# Usage:
#   source scenario-player/sim_env.sh          -- sets REPO_DIR, NGT_DEV_HOME, and the
#                                      sim_setup/sim_reset functions; doesn't
#                                      execute anything on its own.
#   ./scenario-player/sim_env.sh setup|reset   -- run directly instead (see
#                                      sim_setup/sim_reset below for what each does).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

# sim_setup: create/refresh everything under $NGT_DEV_HOME that any engine
# adapter needs -- the fake scenario-player/bin/ toolchain, env.sh, ngtParameters.jsn,
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
    echo "  source ~/airflow3-ngt-venv/bin/activate" >&2
    echo "  source .venv/bin/activate                 # or the plain venv used for pytest" >&2
    return 1
  fi

  mkdir -p "$NGT_DEV_HOME"/{data,logs,cond_auth,calibrationYAML,eos,bin}
  mkdir -p "$NGT_DEV_HOME/cmssw_home/CMSSW_16_0_7_patch1/src"

  # -r-less glob copy: skip subdirs (e.g. a stray __pycache__) instead of
  # letting cp's non-zero exit on them silently abort this function under set -e.
  for f in "$REPO_DIR"/scenario-player/bin/*; do
    [ -f "$f" ] && cp "$f" "$NGT_DEV_HOME/bin/"
  done
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

  # The one place that spells out the fake environment: engine adapters source
  # this instead of re-deriving the same exports themselves.
  cat > "$NGT_DEV_HOME/env.sh" <<ENV
export NGT_DEV_HOME="$NGT_DEV_HOME"
export NGT_PARAMETERS_PATH="$NGT_DEV_HOME/ngtParameters.jsn"
export NGT_CALIBRATION_YAML_DIR="$NGT_DEV_HOME/calibrationYAML"
export NGT_OMS_STUB_RUNS_FILE="$NGT_DEV_HOME/oms_runs.json"
export PATH="$NGT_DEV_HOME/bin:\$PATH"
export PYTHONPATH="$REPO_DIR/tests/stubs:\${PYTHONPATH:-}"

# A real CMSSW environment provides cmsenv/cmsrel as shell *functions*, and bash
# resolves a function before it searches \$PATH -- an exported one survives into
# the job scripts the loops spawn and would run instead of bin/'s fake. Dropping
# them here covers the whole chain, since a function can only arrive by
# inheritance from the shell that sourced this file.
unset -f cmsenv cmsrel cmsDriver.py cmsRun uploadConditions.py 2>/dev/null || true
ENV

  python3 "$REPO_DIR/scenario-player/seed.py" setup

  # Makes the fake `omsapi` package importable by whatever Python process is
  # actually running the engine's workers. Harmless no-op for an engine that
  # doesn't need it.
  pip install -e "$REPO_DIR/tests/stubs" -q
}

# sim_reset: wipe all simulator state -- every fake run/lumisection/EOS file/
# output $NGT_DEV_HOME holds, regardless of which engine (if any) produced or
# is watching it. Does NOT recreate it afterward (call sim_setup for that) and
# does NOT touch any engine's own state -- an engine that keeps run-history
# outside $NGT_DEV_HOME needs its own separate reset too.
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
