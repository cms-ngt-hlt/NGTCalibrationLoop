#!/bin/bash
# Self-contained "live" runner for the Airflow-based NGT calibration loop
# (airflow_dags/ngt_dags.py) against the same fake OMS/EOS/CMSSW toolchain
# dev/live_demo.sh uses (dev/bin/, tests/stubs/omsapi, dev/seed.py) -- so the
# real Airflow scheduler drives real DAG runs, real generated job scripts, and
# real output files flowing Step2 -> Step3 -> Step4 to a fake condDB upload,
# entirely offline. Needs a Linux host (Airflow has no Windows support; WSL
# works fine on Windows).
#
# For a guided, pause-and-confirm walkthrough instead of firing everything at
# once, use dev/airflow_interactive_demo.sh.
#
# Quick start:
#   ./dev/airflow_demo.sh setup
#   ./dev/airflow_demo.sh start
#   ./dev/airflow_demo.sh unpause EcalPedestals
#   ./dev/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52
#   ./dev/airflow_demo.sh ui            # prints the webserver URL + login
#   ./dev/airflow_demo.sh add-ls EcalPedestals 398600 53
#   ./dev/airflow_demo.sh end-run 398600
#   ./dev/airflow_demo.sh status
#   ./dev/airflow_demo.sh stop
#
# $NGT_DEV_HOME (default ./demo-env, a gitignored directory inside this repo)
# holds the fake data/EOS/CMSSW scratch state, same default as dev/seed.py uses
# directly -- delete it any time to reset the demo data. The Airflow instance
# itself (metadata DB, AIRFLOW_HOME) is shared/persistent -- see
# dev/airflow_env.sh and README.md's Airflow setup section for how it was
# provisioned (Postgres role/db + `airflow db migrate` + admin user, one-time).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"
CALIBRATIONS=(SiStripBad EcalPedestals BeamSpot)
WEBSERVER_PORT="${NGT_AIRFLOW_PORT:-8090}"

usage() {
  cat <<USAGE
Usage: $0 <command> [args]

Setup:
  setup                                  Create/refresh \$NGT_DEV_HOME scratch data
                                          (fake EOS/CMSSW/OMS state; safe to re-run)

Running Airflow (one shared webserver+scheduler, tmux sessions NGTAirflow*):
  start                                   Launch webserver (port $WEBSERVER_PORT) + scheduler
  stop                                    Kill both tmux sessions
  status                                  List running NGTAirflow* tmux sessions
  ui                                      Print the webserver URL + admin/admin login
  logs <webserver|scheduler>              Tail -f that process's tmux console log

DAG control:
  unpause <calibration>                   Unpause all 6 DAGs for a calibration
  pause <calibration>                     Pause them again

Driving a fake run (edits \$NGT_DEV_HOME/oms_runs.json + drops fake RAW files,
identical to dev/live_demo.sh -- delegates straight to dev/seed.py):
  seed-run <calibration> <run> [--ls N ...] [--minutes-ago M]
  add-ls <calibration> <run> <ls>
  end-run <run>
  list-runs

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
  if [ ! -f "$HOME/airflow-ngt-venv/bin/activate" ]; then
    echo "No Airflow venv at ~/airflow-ngt-venv -- see README.md's Airflow setup section" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$HOME/airflow-ngt-venv/bin/activate"
  # shellcheck disable=SC1091
  source "$REPO_DIR/dev/airflow_env.sh"
}

demo_env_exports() {
  # Printed as a block and eval'd inside the tmux session, so the scheduler
  # (which is what actually executes task callables under LocalExecutor)
  # inherits the fake toolchain -- same variables dev/live_demo.sh wires in.
  #
  # NOTE: the omsapi stub is deliberately NOT wired in via PYTHONPATH here --
  # Airflow's scheduler parses/executes DAG code in forked/spawned worker
  # subprocesses, and PYTHONPATH set in the launching shell was not reliably
  # observed to reach them in testing. Instead, `cmd_setup` pip-installs
  # tests/stubs (a real, tiny installable package) directly into
  # ~/airflow-ngt-venv, so `import omsapi` resolves normally everywhere
  # without relying on subprocess environment propagation.
  cat <<ENV
export NGT_PARAMETERS_PATH="$NGT_DEV_HOME/ngtParameters.jsn"
export NGT_CALIBRATION_YAML_DIR="$NGT_DEV_HOME/calibrationYAML"
export NGT_OMS_STUB_RUNS_FILE="$NGT_DEV_HOME/oms_runs.json"
export NGT_LOOP_SLEEP_SECONDS="${NGT_LOOP_SLEEP_SECONDS:-10}"
export PATH="$NGT_DEV_HOME/bin:\$PATH"
ENV
}

cmd_setup() {
  activate_venv
  mkdir -p "$NGT_DEV_HOME"/{data,logs,cond_auth,calibrationYAML,eos,bin}
  mkdir -p "$NGT_DEV_HOME/cmssw_home/CMSSW_16_0_7_patch1/src"

  cp "$REPO_DIR"/dev/bin/* "$NGT_DEV_HOME/bin/"
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

  python3 "$REPO_DIR/dev/seed.py" setup

  pip install -e "$REPO_DIR/tests/stubs" -q  # see demo_env_exports' note on why this replaces PYTHONPATH

  echo
  echo "Set up $NGT_DEV_HOME"
  echo "Next: $0 start"
}

tmux_console_log() {
  echo "$NGT_DEV_HOME/logs/tmux_console_airflow_$1.log"
}

cmd_start() {
  activate_venv
  mkdir -p "$NGT_DEV_HOME/logs"

  if ! tmux has-session -t NGTAirflowScheduler 2>/dev/null; then
    local log; log="$(tmux_console_log scheduler)"
    tmux new-session -d -s NGTAirflowScheduler "bash -lc '
      source \"$HOME/airflow-ngt-venv/bin/activate\"
      source \"$REPO_DIR/dev/airflow_env.sh\"
      $(demo_env_exports)
      airflow scheduler 2>&1 | tee \"$log\"
      echo \"(scheduler exited -- press enter to close)\"; read
    '"
    echo "Started NGTAirflowScheduler (console log: $log)"
  else
    echo "NGTAirflowScheduler already running"
  fi

  if ! tmux has-session -t NGTAirflowWebserver 2>/dev/null; then
    local log; log="$(tmux_console_log webserver)"
    tmux new-session -d -s NGTAirflowWebserver "bash -lc '
      source \"$HOME/airflow-ngt-venv/bin/activate\"
      source \"$REPO_DIR/dev/airflow_env.sh\"
      $(demo_env_exports)
      airflow webserver -p $WEBSERVER_PORT 2>&1 | tee \"$log\"
      echo \"(webserver exited -- press enter to close)\"; read
    '"
    echo "Started NGTAirflowWebserver on port $WEBSERVER_PORT (console log: $log)"
  else
    echo "NGTAirflowWebserver already running"
  fi

  echo
  echo "Give the scheduler ~10-15s to parse airflow_dags/ before unpausing DAGs."
  cmd_ui
}

cmd_ui() {
  echo "Airflow UI: http://localhost:$WEBSERVER_PORT  (login: admin / admin)"
}

cmd_status() {
  tmux list-sessions 2>/dev/null | grep '^NGTAirflow' || echo "(no NGTAirflow* tmux sessions running)"
}

cmd_logs() {
  local which="$1"
  case "$which" in
    webserver|scheduler) ;;
    *) echo "Usage: $0 logs <webserver|scheduler>" >&2; exit 1 ;;
  esac
  local f; f="$(tmux_console_log "$which")"
  [ -f "$f" ] || { echo "No log yet at $f -- has '$0 start' been run?" >&2; exit 1; }
  tail -f "$f"
}

cmd_stop() {
  for session in NGTAirflowScheduler NGTAirflowWebserver; do
    if tmux has-session -t "$session" 2>/dev/null; then
      tmux kill-session -t "$session"
      echo "Killed $session"
    fi
  done
}

cmd_pause_unpause() {
  local action="$1" calibration="$2"
  require_calibration "$calibration"
  activate_venv
  local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
  for step in 2 3 4; do
    for kind in latch process; do
      airflow dags "$action" "ngt_step${step}_${lower}_${kind}"
    done
  done
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  ui) cmd_ui ;;
  logs) cmd_logs "${2:?webserver or scheduler required}" ;;
  unpause) cmd_pause_unpause unpause "${2:?calibration required}" ;;
  pause) cmd_pause_unpause pause "${2:?calibration required}" ;;
  seed-run)
    calibration="${2:?calibration required}"; run="${3:?run number required}"; shift 3 || true
    activate_venv
    python3 "$REPO_DIR/dev/seed.py" seed-run --calibration "$calibration" --run "$run" "$@"
    ;;
  add-ls)
    activate_venv
    python3 "$REPO_DIR/dev/seed.py" add-ls --calibration "${2:?calibration required}" --run "${3:?run required}" --ls "${4:?ls number required}"
    ;;
  end-run)
    activate_venv
    python3 "$REPO_DIR/dev/seed.py" end-run --run "${2:?run number required}"
    ;;
  list-runs)
    activate_venv
    python3 "$REPO_DIR/dev/seed.py" list-runs
    ;;
  *) usage; exit 1 ;;
esac
