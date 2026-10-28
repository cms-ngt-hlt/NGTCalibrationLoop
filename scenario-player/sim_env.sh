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
#   source scenario-player/sim_env.sh          -- sets REPO_DIR, NGT_DEV_HOME, NGT_INSTANCE, and the
#                                      sim_setup/sim_reset/ngt_tmux_new_session
#                                      functions; doesn't execute anything on its own.
#   ./scenario-player/sim_env.sh setup|reset   -- run directly instead (see
#                                      sim_setup/sim_reset below for what each does).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

# A name for this checkout and scratch environment, that engine adapters use to keep several of
# them apart -- live_demo.sh names its tmux sessions after it: $REPO_DIR's basename plus a short
# hash of its full path and $NGT_DEV_HOME (the code is per checkout, the state a session works on per $NGT_DEV_HOME).
# NGT_INSTANCE_HASH is the same hash as a number.
if [ -z "${NGT_INSTANCE_HASH:-}" ]; then
  NGT_INSTANCE_HASH="$(printf '%s' "$REPO_DIR|${NGT_DEV_HOME%/}" | cksum | cut -d' ' -f1)"
fi
if [ -z "${NGT_INSTANCE:-}" ]; then
  _ngt_base="$(basename "$REPO_DIR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '_')"
  NGT_INSTANCE="${_ngt_base:0:30}_$(printf '%06x' $((NGT_INSTANCE_HASH % 16777216)))"
  unset _ngt_base
fi
export NGT_INSTANCE NGT_INSTANCE_HASH

# sim_setup: create/refresh everything under $NGT_DEV_HOME that any engine
# adapter needs -- env.sh (which puts this checkout's fake scenario-player/bin/
# toolchain on $PATH), ngtParameters.jsn, and (via scenario-player/seed.py,
# itself fully engine-agnostic) the per-calibration fake EOS layout and an empty
# fake OMS runs file. Safe to re-run.
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

  mkdir -p "$NGT_DEV_HOME"/{data,logs,cond_auth,calibrationYAML,eos,faults}
  mkdir -p "$NGT_DEV_HOME/cmssw_home/CMSSW_16_0_7_patch1/src"

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
export PATH="$REPO_DIR/scenario-player/bin:\$PATH"
# scenario-player/ for the fake omsapi's live fault path, which imports faults.py.
export PYTHONPATH="$REPO_DIR/tests/stubs:$REPO_DIR/scenario-player:\${PYTHONPATH:-}"

# Where seed.py's arm_fault() leaves faults; the fakes only look for them when this is set.
export NGT_FAULTS_DIR="$NGT_DEV_HOME/faults"

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
  _install_omsapi_stub
}

# _install_omsapi_stub: `pip install -e tests/stubs`, skipped when the active venv already
# has it from this checkout with the same tests/stubs/pyproject.toml (recorded in a stamp
# file) -- reinstalling an installed editable package races with a second setup sharing
# the venv. Edits to omsapi/ itself need no reinstall.
_install_omsapi_stub() {
  local stub_dir="$REPO_DIR/tests/stubs"
  local stamp="$VIRTUAL_ENV/.ngt_omsapi_stub.stamp"
  (
    command -v flock >/dev/null 2>&1 && flock 9
    local want="$stub_dir $(sha256sum "$stub_dir/pyproject.toml" | cut -d' ' -f1)"
    if [ "$(cat "$stamp" 2>/dev/null)" = "$want" ] \
        && python -c "import importlib.metadata as m; m.version('ngt-omsapi-stub')" 2>/dev/null; then
      exit 0
    fi
    pip install -e "$stub_dir" -q
    echo "$want" > "$stamp"
  ) 9>"$VIRTUAL_ENV/.ngt_omsapi_stub.lock"
}

# sim_reset: wipe all simulator state -- every fake run/lumisection/EOS file/
# output $NGT_DEV_HOME holds, regardless of which engine (if any) produced or
# is watching it. Does NOT recreate it afterward (call sim_setup for that) and
# does NOT touch any engine's own state -- an engine that keeps run-history
# outside $NGT_DEV_HOME needs its own separate reset too.
# Stop the engine first: running processes keep pointing at the deleted state.
sim_reset() {
  echo "Removing $NGT_DEV_HOME"
  rm -rf "$NGT_DEV_HOME"
}

# ngt_tmux_new_session: `tmux new-session` (same arguments) for an engine adapter's
# long-running sessions. Whichever tmux client runs first starts the tmux server, and the
# server hands every shell it ever spawns -- an operator's own new windows included -- that
# client's environment. Started from an adapter with this checkout's settings exported
# (NGT_INSTANCE, NGT_DEV_HOME, ...), it would make them the default of every later shell,
# where another checkout's scripts would take them for their own and act on this checkout's
# state. So the client runs without any NGT_* variable, and a session's command has to bring
# its own: live_demo.sh's sources $NGT_DEV_HOME/env.sh.
ngt_tmux_new_session() {
  local v unset_args=()
  for v in $(compgen -e); do
    case "$v" in NGT_*) unset_args+=(-u "$v") ;; esac
  done
  env "${unset_args[@]}" tmux new-session "$@"
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
