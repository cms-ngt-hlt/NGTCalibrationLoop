#!/bin/bash
# Self-contained "live" runner for NGTLoopStep2/3/4.py against a fake OMS/EOS/CMSSW
# toolchain (see scenario-player/bin/), so the FSM loops can be watched producing actual
# state transitions, job scripts, and output files -- without any CERN network
# access, real CMSSW, or the real omsapi client.
#
# For pure logic testing (no live processes, no tmux) use pytest instead -- see
# README.md's "Running the FSM logic locally / tests" section.
#
# Usage:
# See README.md or usage function below 
#
# $NGT_DEV_HOME (default ./demo-env, i.e. a gitignored directory inside this repo)
# holds all scratch state -- delete it any time to reset the demo environment.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# NGT_DEV_HOME, NGT_INSTANCE and ngt_tmux_new_session are defined here (if unset)
# Note: the disable silences sourcing file from runtime-only path
# shellcheck disable=SC1091
source "$REPO_DIR/scenario-player/sim_env.sh"
# Every session of this checkout starts with it, so two checkouts' loops can run side by side.
TMUX_PREFIX="NGTDemo_${NGT_INSTANCE}_"
CALIBRATIONS=(SiStripBad EcalPedestals BeamSpot)
PLAYER="$REPO_DIR/scenario-player/scenario_player.py"

usage() {
  cat <<USAGE
Usage: $0 <command> [args]

Setup:
  setup                                  Create/refresh the \$NGT_DEV_HOME scratch environment
                                          (safe to re-run; \$NGT_DEV_HOME default: ./demo-env)
  reset                                  Stop this instance's loops, then delete \$NGT_DEV_HOME
                                          entirely -- every fake run, EOS file, output and log.
                                          \`setup\` (or \`play\`) recreates it

Configuration (overridable via environment variables):
  NGT_DEV_HOME              scratch state directory          (default: $REPO_DIR/demo-env)
  NGT_INSTANCE              names tmux sessions              (default: see sim_env.sh)
  NGT_LOOP_SLEEP_SECONDS    Step 3/4 poll interval, seconds  (default: 5; production default is 60)

Running the loops (each in its own tmux session, ${TMUX_PREFIX}{2,3,4}_<calibration>):
  start2 <calibration>                   Launch Step 2 only
  start3 <calibration>                   Launch Step 3 only
  start4 <calibration>                   Launch Step 4 only
  start-all <calibration>                Launch all three
  stop-all <calibration>                 Kill all three tmux sessions for a calibration
  status                                 List this checkout's running ${TMUX_PREFIX}* sessions
  logs <calibration> <2|3|4>             Tail -f that step's combined log

Driving a fake run (edits \$NGT_DEV_HOME/oms_runs.json + drops fake RAW files):
  seed-run <calibration> <run> [--ls N ...] [--minutes-ago M]
                                          Latch a new live run, optionally with LS files already present
  add-ls <calibration> <run> <ls>        Drop one more fake RAW LS file for an existing run
  end-run <run>                          Mark the run as ended in the fake OMS
  list-runs                              Show all runs currently seeded
  play <scenario.yaml> [options]         Play a whole timeline from a scenario file instead
                                          of firing the three verbs above by hand. Sets up
                                          \$NGT_DEV_HOME if it isn't there and starts Step
                                          2/3/4 for the calibrations that scenario feeds,
                                          then passes every argument straight to
                                          scenario_player.py (--interactive, --monitor,
                                          --speed, --dry-run -- run it with --help)

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
  # Note: the disable silences sourcing file from runtime-only path
  # shellcheck disable=SC1091
  source "$REPO_DIR/.venv/bin/activate"
}

cmd_setup() {
  activate_venv
  sim_setup

  echo
  echo "Set up $NGT_DEV_HOME"
  echo "Next: $0 start-all EcalPedestals"
}

cmd_reset() {
  local calibration
  for calibration in "${CALIBRATIONS[@]}"; do
    cmd_stop_all "$calibration"
  done

  # from sim_env.sh
  # no venv needed
  sim_reset

  echo "Next: $0 setup"
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
  local session="${TMUX_PREFIX}${step}_${calibration}"
  if tmux has-session -t "=$session" 2>/dev/null; then
    echo "$session is already running (tmux attach -t $session)"
    return 0
  fi
  if [ ! -f "$NGT_DEV_HOME/env.sh" ]; then
    echo "No $NGT_DEV_HOME/env.sh -- run: $0 setup" >&2
    exit 1
  fi
  local console_log
  console_log="$(console_log_path "$step" "$calibration")"
  mkdir -p "$(dirname "$console_log")"
  # Plain -c, not -lc: a login shell would re-source the operator's dotfiles,
  # whose cmsenv/cmsrel functions outrank the fake toolchain in $PATH. Everything the
  # loops need comes from env.sh and this command line, so ngt_tmux_new_session (sim_env.sh)
  # can keep this checkout's settings out of the tmux server's environment.
  ngt_tmux_new_session -d -s "$session" "bash -c '
    cd \"$REPO_DIR\"
    source \"$REPO_DIR/.venv/bin/activate\"
    source \"$NGT_DEV_HOME/env.sh\"
    export NGT_LOOP_SLEEP_SECONDS=${NGT_LOOP_SLEEP_SECONDS:-5}
    python NGTLoopStep${step}.py -c \"$calibration\" 2>&1 | tee \"$console_log\"
    echo \"(process exited -- press enter to close)\"
    read
  '"
  echo "Started $session (tmux attach -t $session, Ctrl-b d to detach; console log: $console_log)"
}

cmd_start_all() {
  local calibration="$1"
  start_step 2 "$calibration"
  start_step 3 "$calibration"
  start_step 4 "$calibration"
}

cmd_status() {
  tmux list-sessions 2>/dev/null | grep "^$TMUX_PREFIX" || echo "(no ${TMUX_PREFIX}* tmux sessions running)"
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
    local session="${TMUX_PREFIX}${step}_${calibration}"
    if tmux has-session -t "=$session" 2>/dev/null; then
      tmux kill-session -t "=$session"
      echo "Killed $session"
    fi
  done
}

cmd_play() {
  # The player's flags that play nothing need no scratch environment and no loops -- and
  # --help would otherwise reach the calibration loop below as a calibration named "usage:".
  local arg
  for arg in "$@"; do
    case "$arg" in
      -h|--help|--dry-run|--only-list-calibrations)
        activate_venv
        exec python3 "$PLAYER" "$@"
        ;;
    esac
  done
  activate_venv

  if [ ! -f "$NGT_DEV_HOME/ngtParameters.jsn" ]; then
    echo "(no scratch environment at $NGT_DEV_HOME yet -- setting one up first)"
    cmd_setup
    echo
  fi

  # Get the scenario's calibrations with --only-list-calibrations (ignores other args)
  # then start all these calibrations.
  local calibrations
  calibrations="$(python3 "$PLAYER" "$@" --only-list-calibrations)"
  while read -r calibration; do
    [ -n "$calibration" ] || continue
    cmd_start_all "$calibration"
  done <<<"$calibrations"
  echo

  python3 "$PLAYER" "$@"
}

case "${1:-}" in
  setup) cmd_setup ;;
  reset) cmd_reset ;;
  start2) start_step 2 "${2:?calibration required}" ;;
  start3) start_step 3 "${2:?calibration required}" ;;
  start4) start_step 4 "${2:?calibration required}" ;;
  start-all) cmd_start_all "${2:?calibration required}" ;;
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
  play)
    # Everything after `play` goes to scenario_player.py untouched -- this verb never
    # re-parses its flags. The scenario path is resolved relative to your CWD, exactly
    # as if you'd run the player directly.
    shift
    cmd_play "$@"
    ;;
  *) usage; exit 1 ;;
esac
