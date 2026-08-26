#!/bin/bash
# Self-contained "live" runner for NGTLoopStep2/3/4.py against a fake OMS/EOS/CMSSW
# toolchain (see scenario-player/bin/), so the FSM loops can be watched running for real --
# real state transitions, real generated job scripts, real output files -- without
# any CERN network access, real CMSSW, or the real omsapi client.
#
# For pure logic testing (no live processes, no tmux) use pytest instead -- see
# README.md's "Running the FSM logic locally / tests" section.
#
# Quick start:
#   ./scenario-player/live_demo.sh setup
#   ./scenario-player/live_demo.sh start-all EcalPedestals
#   ./scenario-player/live_demo.sh seed-run EcalPedestals 398600 --ls 51 52
#   tmux attach -t NGTDemo2_EcalPedestals        # Ctrl-b d to detach
#   ./scenario-player/live_demo.sh add-ls EcalPedestals 398600 53
#   ./scenario-player/live_demo.sh end-run 398600
#   ./scenario-player/live_demo.sh status
#   ./scenario-player/live_demo.sh stop-all EcalPedestals
#
# $NGT_DEV_HOME (default ./demo-env, i.e. a gitignored directory inside this repo)
# holds all scratch state -- delete it any time to reset the demo environment.
#
# Configuration (overridable via environment variables):
#   NGT_DEV_HOME              scratch state directory        (default: $REPO_DIR/demo-env)
#   NGT_LOOP_SLEEP_SECONDS    Step 3/4 poll interval, seconds  (default: 5; production default is 60)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"
# Fake, unvalidated placeholder values -- nothing in scenario-player/bin/ reads them, they're
# only written into the fake ngtParameters.jsn / used for one directory name below.
CMSSW_VERSION="CMSSW_16_0_7_patch1"
SCRAM_ARCH="el8_amd64_gcc13"
GLOBAL_TAG="160X_dataRun3_ExpressNGT_v0"
CALIBRATIONS=(SiStripBad EcalPedestals BeamSpot)

usage() {
  cat <<USAGE
Usage: $0 <command> [args]

Setup:
  setup                                  Create/refresh the \$NGT_DEV_HOME scratch environment
                                          (safe to re-run; \$NGT_DEV_HOME default: ./demo-env)

Running the loops (each in its own tmux session, NGTDemo{2,3,4}_<calibration>):
  start2 <calibration>                   Launch Step 2 only
  start3 <calibration>                   Launch Step 3 only
  start4 <calibration>                   Launch Step 4 only
  start-all <calibration>                Launch all three
  stop-all <calibration>                 Kill all three tmux sessions for a calibration
  status                                 List running NGTDemo* tmux sessions
  logs <calibration> <2|3|4>             Tail -f that step's combined log

Driving a fake run (edits \$NGT_DEV_HOME/oms_runs.json + drops fake RAW files):
  seed-run <calibration> <run> [--ls N ...] [--minutes-ago M]
                                          Latch a new live run, optionally with LS files already present
  add-ls <calibration> <run> <ls>        Drop one more fake RAW LS file for an existing run
  end-run <run>                          Mark the run as ended in the fake OMS
  list-runs                              Show all runs currently seeded

calibration is one of: ${CALIBRATIONS[*]}
USAGE
}

require_calibration() {
  local c="$1"
  for known in "${CALIBRATIONS[@]}"; do
    [ "$c" = "$known" ] && return 0
  done
  echo "Unknown calibration '$c' -- must be one of: ${CALIBRATIONS[*]}" >&2
  exit 1
}

activate_venv() {
  if [ ! -f "$REPO_DIR/.venv/bin/activate" ]; then
    echo "No venv at $REPO_DIR/.venv -- run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$REPO_DIR/.venv/bin/activate"
}

cmd_setup() {
  activate_venv
  mkdir -p "$NGT_DEV_HOME"/{data,logs,cond_auth,calibrationYAML,eos,bin}
  mkdir -p "$NGT_DEV_HOME/cmssw_home/$CMSSW_VERSION/src"  # Step4 assumes this pre-exists (no cmsrel of its own)

  cp "$REPO_DIR"/scenario-player/bin/* "$NGT_DEV_HOME/bin/"
  chmod +x "$NGT_DEV_HOME"/bin/*

  cat > "$NGT_DEV_HOME/ngtParameters.jsn" <<JSON
{
    "SCRAM_ARCH": "$SCRAM_ARCH",
    "CMSSW_VERSION": "$CMSSW_VERSION",
    "GLOBAL_TAG": "$GLOBAL_TAG",
    "DATA_BASE_PATH": "$NGT_DEV_HOME/data",
    "LOG_BASE_PATH": "$NGT_DEV_HOME/logs",
    "COND_AUTH_PATH": "$NGT_DEV_HOME/cond_auth"
}
JSON

  python3 "$REPO_DIR/scenario-player/seed.py" setup

  echo
  echo "Set up $NGT_DEV_HOME"
  echo "Next: $0 start-all EcalPedestals"
}

console_log_path() {
  # Where a tmux session's raw console output (stdout/stderr of the python
  # process itself, as distinct from NGTLoopStepN_ALL.log which is written by
  # the Python logging module) is tee'd to.
  local step="$1" calibration="$2"
  echo "$NGT_DEV_HOME/logs/tmux_console_Step${step}_${calibration}.log"
}

start_step() {
  local step="$1" calibration="$2"
  require_calibration "$calibration"
  local session="NGTDemo${step}_${calibration}"
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "$session is already running (tmux attach -t $session)"
    return 0
  fi
  local console_log
  console_log="$(console_log_path "$step" "$calibration")"
  mkdir -p "$(dirname "$console_log")"
  tmux new-session -d -s "$session" "bash -lc '
    cd \"$REPO_DIR\"
    source \"$REPO_DIR/.venv/bin/activate\"
    export NGT_PARAMETERS_PATH=\"$NGT_DEV_HOME/ngtParameters.jsn\"
    export NGT_CALIBRATION_YAML_DIR=\"$NGT_DEV_HOME/calibrationYAML\"
    export NGT_OMS_STUB_RUNS_FILE=\"$NGT_DEV_HOME/oms_runs.json\"
    export NGT_LOOP_SLEEP_SECONDS=${NGT_LOOP_SLEEP_SECONDS:-5}
    export PATH=\"$NGT_DEV_HOME/bin:\$PATH\"
    export PYTHONPATH=\"$REPO_DIR/tests/stubs:\${PYTHONPATH:-}\"
    python NGTLoopStep${step}.py -c \"$calibration\" 2>&1 | tee \"$console_log\"
    echo \"(process exited -- press enter to close)\"
    read
  '"
  echo "Started $session (tmux attach -t $session, Ctrl-b d to detach; console log: $console_log)"
}

cmd_status() {
  tmux list-sessions 2>/dev/null | grep '^NGTDemo' || echo "(no NGTDemo* tmux sessions running)"
}

cmd_logs() {
  local calibration="$1" step="$2"
  require_calibration "$calibration"
  local f="$NGT_DEV_HOME/logs/$calibration/NGTLoopStep${step}_ALL.log"
  [ -f "$f" ] || { echo "No log yet at $f -- has $0 start$step $calibration been run?" >&2; exit 1; }
  tail -f "$f"
}

cmd_stop_all() {
  local calibration="$1"
  require_calibration "$calibration"
  for step in 2 3 4; do
    local session="NGTDemo${step}_${calibration}"
    if tmux has-session -t "$session" 2>/dev/null; then
      tmux kill-session -t "$session"
      echo "Killed $session"
    fi
  done
}

case "${1:-}" in
  setup) cmd_setup ;;
  start2) start_step 2 "${2:?calibration required}" ;;
  start3) start_step 3 "${2:?calibration required}" ;;
  start4) start_step 4 "${2:?calibration required}" ;;
  start-all)
    start_step 2 "${2:?calibration required}"
    start_step 3 "$2"
    start_step 4 "$2"
    ;;
  stop-all) cmd_stop_all "${2:?calibration required}" ;;
  status) cmd_status ;;
  logs) cmd_logs "${2:?calibration required}" "${3:?2, 3 or 4 required}" ;;
  seed-run)
    calibration="${2:?calibration required}"; run="${3:?run number required}"; shift 3 || true
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" seed-run --calibration "$calibration" --run "$run" "$@"
    ;;
  add-ls)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" add-ls --calibration "${2:?calibration required}" --run "${3:?run required}" --ls "${4:?ls number required}"
    ;;
  end-run)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" end-run --run "${2:?run number required}"
    ;;
  list-runs)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" list-runs
    ;;
  *) usage; exit 1 ;;
esac
