#!/bin/bash
# Self-contained "live" runner for the Airflow-based NGT calibration loop
# against the same fake OMS/EOS/CMSSW toolchain scenario-player/sim_env.sh's `sim_setup`
# provides (scenario-player/bin/, tests/stubs/omsapi, scenario-player/seed.py) -- so the real Airflow
# scheduler drives real DAG runs, real generated job scripts, and real output
# files flowing Step2 -> Step3 -> Step4 to a fake condDB upload, entirely
# offline. Needs a Linux host (Airflow has no Windows support; WSL works fine
# on Windows).
#
# Manages ONE shared Airflow instance that runs BOTH DAG designs at once --
# airflow_automation/airflow_dags/ngt_dags_per_file.py (5 DAGs -- run-detector, file-detector,
# and one per-file-process DAG per calibration) and
# airflow_automation/airflow_dags/ngt_dags_watch.py (4 DAGs -- all-static-asset, AssetWatcher-
# driven, zero cron; see its module docstring) -- so the same seed-run/
# add-ls/end-run/scenario_player.py-driven fake run can be pointed at
# whichever design's DAGs are unpaused, to compare them head-to-head.
# IMPORTANT: only one design's run-discovery may be unpaused at a time -- they
# both share the on-disk DATA/<cal>/run<N>/ working-dir dedup and will race.
#
# This file is the Airflow-*specific* engine adapter: everything about
# actually simulating a live run (seed-run/add-ls/end-run/list-runs below)
# just forwards to scenario-player/seed.py, which knows nothing about Airflow -- see
# scenario-player/sim_env.sh's module docstring for why that split exists.
#
# For a guided, pause-and-confirm walkthrough instead of firing everything at
# once, use perfile_interactive_demo.sh (ngt_dags_per_file.py -- see
# per_file_scheduling_design.md) or watch_interactive_demo.sh (ngt_dags_watch.py). For an unattended, timed sequence of runs/
# lumisections (e.g. for a scripted test run against whatever engine/design
# is currently `start`ed/unpaused), use scenario-player/scenario_player.py -- see
# scenario-player/scenarios/.
#
# Quick start:
#   ./airflow_automation/airflow_demo/airflow_demo.sh setup
#   ./airflow_automation/airflow_demo/airflow_demo.sh start
#   ./airflow_automation/airflow_demo/airflow_demo.sh unpause-perfile
#   ./airflow_automation/airflow_demo/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52
#   ./airflow_automation/airflow_demo/airflow_demo.sh ui            # prints the webserver URL
#   ./airflow_automation/airflow_demo/airflow_demo.sh add-ls EcalPedestals 398600 53
#   ./airflow_automation/airflow_demo/airflow_demo.sh end-run 398600
#   ./airflow_automation/airflow_demo/airflow_demo.sh status
#   ./airflow_automation/airflow_demo/airflow_demo.sh stop
#
# $NGT_DEV_HOME (default ./demo-env, a gitignored directory inside this repo)
# holds the fake data/EOS/CMSSW scratch state, same default as scenario-player/seed.py
# uses directly -- delete it any time to reset the demo data. The Airflow
# instance itself (metadata DB, AIRFLOW_HOME) is shared/persistent -- see
# airflow_env.sh and README.md's Airflow setup section for how it was
# provisioned (Postgres role/db + `airflow db migrate` + admin user, one-time).

set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scenario-player" && pwd)/sim_env.sh"
CALIBRATIONS=(SiStripBad EcalPedestals BeamSpot)

usage() {
  cat <<USAGE
Usage: $0 <command> [args]

Setup:
  setup                                  Create/refresh \$NGT_DEV_HOME scratch data
                                          (fake EOS/CMSSW/OMS state; safe to re-run)

Running Airflow (one shared instance, tmux sessions NGTAirflow*):
  start                                   Launch scheduler + dag-processor + API server + triggerer
  stop                                    Kill all four tmux sessions
  status                                  List running NGTAirflow* tmux sessions
  ui                                      Print the Airflow UI URL
  logs <scheduler|dag-processor|api|triggerer>   Tail -f that process's tmux console log

DAG control:
  unpause-perfile                         Unpause ngt_dags_per_file.py's 5 DAGs (all calibrations)
  pause-perfile                           Pause them again
  unpause-watch                           Unpause ngt_dags_watch.py's 4 DAGs (starts the AssetWatchers)
  pause-watch                             Pause them again (stops the AssetWatchers)

Resetting state (see scenario-player/sim_env.sh's setup/reset for the simulator's OWN state --
\$NGT_DEV_HOME -- which none of these touch, except reset-scenario):
  reset-perfile                           Clear DAG-run history for ngt_dags_per_file.py's 5 DAGs
  reset-watch                             Clear DAG-run history for ngt_dags_watch.py's 4 DAGs
                                          (also drops the ngt://runs / ngt://files/* Assets +
                                          their AssetStateStore rows)
  reset-airflow                           Full reset: drop + recreate the whole metadata DB
                                          (both designs' history, variables, connections --
                                          everything) and re-migrate. Back to freshly-installed.
  reset-scenario                          Wipe + recreate \$NGT_DEV_HOME (scenario-player/sim_env.sh's own
                                          reset+setup), stopping/restarting Airflow around it if
                                          running -- calling sim_env.sh's reset directly while
                                          Airflow's processes are live can leave in-flight
                                          deferred work stuck; this is the safe way to do it from
                                          this adapter. Does NOT touch DAG-run history -- pair
                                          with reset-perfile/-watch/-airflow if you want that too.

Driving a fake run (edits \$NGT_DEV_HOME/oms_runs.json + drops fake RAW files,
identical to any other engine adapter -- delegates straight to scenario-player/seed.py):
  seed-run <calibration> <run> [--ls N ...] [--minutes-ago M]
  add-ls <calibration> <run> <ls>
  end-run <run>
  list-runs

Injecting failures (writes \$NGT_DEV_HOME/faults/<target>.json, polled by
tests/stubs/omsapi / scenario-player/bin/cmsRun / scenario-player/bin/uploadConditions.py -- see
scenario-player/faults.py and scenario-player/scenarios/fault_injection_demo.yaml
for the equivalent scenario-YAML \`faults:\` syntax):
  arm-fault --target <oms|cmsrun|upload_conditions> --mode <mode> [options...]
            (oms: --mode connection_error|timeout|http_error|malformed_json
                  [--status N] [--run N] [--times N|unlimited] [--message TEXT]
             cmsrun/upload_conditions: --mode exit_code --exit-code N
                  --calibration <calibration> [--run N] [--step step2|step3|step4]
                  [--times N|unlimited] [--message TEXT])
  list-faults [--target <oms|cmsrun|upload_conditions>]
  clear-faults [--target <oms|cmsrun|upload_conditions>]

calibration is one of: ${CALIBRATIONS[*]}
USAGE
}

activate_venv() {
  if [ ! -f "$HOME/airflow3-ngt-venv/bin/activate" ]; then
    echo "No Airflow venv at ~/airflow3-ngt-venv -- see README.md's Airflow setup section" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$HOME/airflow3-ngt-venv/bin/activate"
  # shellcheck disable=SC1091
  source "$REPO_DIR/airflow_automation/airflow_demo/airflow_env.sh"
}

demo_env_exports() {
  # Printed as a block and eval'd inside the tmux session, so the scheduler
  # (which is what actually executes task callables under LocalExecutor)
  # inherits the fake toolchain -- same variables any engine adapter would
  # need to wire in for its own workers.
  #
  # NOTE: the omsapi stub is deliberately NOT wired in via PYTHONPATH here --
  # Airflow's scheduler parses/executes DAG code in forked/spawned worker
  # subprocesses, and PYTHONPATH set in the launching shell was not reliably
  # observed to reach them in testing. Instead, `sim_setup` (scenario-player/sim_env.sh)
  # pip-installs tests/stubs (a real, tiny installable package) directly into
  # ~/airflow3-ngt-venv, so `import omsapi` resolves normally everywhere
  # without relying on subprocess environment propagation.
  # NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS: read by ngt_dags_per_file.py's
  # wait_for_files task (which runs in the triggerer -- this block reaches
  # it too, start_component uses it for all four processes uniformly).
  # Production default (triggers.DEFAULT_RUN_END_GRACE_SECONDS) is 30 min;
  # 20s here is comfortably above NGT_LOOP_SLEEP_SECONDS/NGT_FILE_POLL_SECONDS
  # (10s/5s) without racing a normal in-flight cycle, so a scenario's
  # end-run resolves in seconds instead of minutes.
  cat <<ENV
export NGT_PARAMETERS_PATH="$NGT_DEV_HOME/ngtParameters.jsn"
export NGT_CALIBRATION_YAML_DIR="$NGT_DEV_HOME/calibrationYAML"
export NGT_OMS_STUB_RUNS_FILE="$NGT_DEV_HOME/oms_runs.json"
export NGT_OMS_STUB_FAULTS_FILE="$NGT_DEV_HOME/faults/oms.json"
export NGT_FAULTS_DIR="$NGT_DEV_HOME/faults"
export NGT_LOOP_SLEEP_SECONDS="${NGT_LOOP_SLEEP_SECONDS:-10}"
export NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS="${NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS:-20}"
export PATH="$NGT_DEV_HOME/bin:\$PATH"
ENV
}

cmd_setup() {
  activate_venv
  sim_setup
  echo
  echo "Set up $NGT_DEV_HOME"
  echo "Next: $0 start"
}

tmux_console_log() {
  echo "$NGT_DEV_HOME/logs/tmux_console_airflow_$1.log"
}

start_component() {
  local session="$1" label="$2"; shift 2
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "$session already running"
    return
  fi
  local log; log="$(tmux_console_log "$label")"
  tmux new-session -d -s "$session" "bash -lc '
    source \"$HOME/airflow3-ngt-venv/bin/activate\"
    source \"$REPO_DIR/airflow_automation/airflow_demo/airflow_env.sh\"
    $(demo_env_exports)
    $* 2>&1 | tee \"$log\"
    echo \"($label exited -- press enter to close)\"; read
  '"
  echo "Started $session (console log: $log)"
}

cmd_start() {
  activate_venv
  mkdir -p "$NGT_DEV_HOME/logs"

  # Airflow 3 splits DAG parsing into its own mandatory process (the
  # scheduler no longer spawns it itself, unlike Airflow 2) -- the scheduler,
  # dag-processor and API server are the baseline. The triggerer is a fourth,
  # hosting ngt_dags_per_file.py's deferred wait_for_files task and
  # ngt_dags_watch.py's AssetWatchers (see their module docstrings) --
  # started unconditionally since it's harmless, near-zero-overhead idle
  # time if those designs' DAGs are paused/unused.
  start_component NGTAirflowScheduler scheduler airflow scheduler
  start_component NGTAirflowDagProcessor dag-processor airflow dag-processor
  start_component NGTAirflowApiServer api "airflow api-server -p $NGT_AIRFLOW_PORT"
  start_component NGTAirflowTriggerer triggerer airflow triggerer

  echo
  echo "Give the dag-processor ~10-15s to parse airflow_automation/airflow_dags/ before unpausing DAGs."
  cmd_ui
}

cmd_ui() {
  echo "Airflow UI: http://localhost:$NGT_AIRFLOW_PORT"
  echo "  (Simple Auth Manager, all-admins mode for this offline dev instance -- no login needed)"
}

cmd_status() {
  tmux list-sessions 2>/dev/null | grep '^NGTAirflow' || echo "(no NGTAirflow* tmux sessions running)"
}

cmd_logs() {
  local which="$1"
  case "$which" in
    api|scheduler|dag-processor|triggerer) ;;
    *) echo "Usage: $0 logs <scheduler|dag-processor|api|triggerer>" >&2; exit 1 ;;
  esac
  local f; f="$(tmux_console_log "$which")"
  [ -f "$f" ] || { echo "No log yet at $f -- has '$0 start' been run?" >&2; exit 1; }
  tail -f "$f"
}

cmd_stop() {
  for session in NGTAirflowScheduler NGTAirflowDagProcessor NGTAirflowApiServer NGTAirflowTriggerer; do
    if tmux has-session -t "$session" 2>/dev/null; then
      tmux kill-session -t "$session"
      echo "Killed $session"
    fi
  done
}

cmd_pause_unpause_perfile() {
  # No calibration argument needed: covers all calibrations in one call.
  # run_detector/file_detector are each one generic DAG (calibration comes
  # from trigger conf, not the DAG definition -- see ngt_dags_per_file.py's
  # module docstring), but process is one DAG *per* calibration
  # (ngt_perfile_process_<calibration>) so it appears separately in the UI --
  # loop over CALIBRATIONS for those specifically.
  local action="$1"
  activate_venv
  for dag_id in ngt_perfile_run_detector ngt_perfile_file_detector; do
    airflow dags "$action" -y "$dag_id"
  done
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags "$action" -y "ngt_perfile_process_${lower}"
  done
}

cmd_pause_unpause_watch() {
  # ngt_dags_watch.py's 4 DAGs (the all-static-asset AssetWatcher design -- see
  # its module docstring). AssetWatchers only run while their consuming DAG is
  # unpaused, so this is also what starts/stops the 4 watchers in the
  # triggerer. Unpause the process DAGs (file watchers) first, then the primer
  # (run watcher), so the file watchers are live before a run is discovered.
  local action="$1"
  activate_venv
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags "$action" -y "ngt_watch_process_${lower}"
  done
  airflow dags "$action" -y "ngt_watch_run_primer"
}

cmd_reset_perfile() {
  activate_venv
  for dag_id in ngt_perfile_run_detector ngt_perfile_file_detector; do
    airflow dags delete -y "$dag_id"
  done
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags delete -y "ngt_perfile_process_${lower}"
  done
  echo "Cleared DAG-run history for all 5 ngt_perfile_* DAGs (ngt_dags_per_file.py)."
}

_reset_ngt_assets() {
  # $1 label; $2/$3 AssetModel.uri LIKE patterns to remove. asset_state_store rows go
  # with the AssetModel via ON DELETE CASCADE. A dev-script admin action, not task code
  # (cf. airflow_automation/airflow_dags/airflow_api.py's "no ORM from tasks"); skipped with a
  # warning if the ORM shape has drifted rather than failing the whole reset.
  local label="$1" uri_a="$2" uri_b="$3"
  URI_A="$uri_a" URI_B="$uri_b" python3 - <<'PY' || echo "  (Asset row cleanup skipped for $label -- use reset-airflow for a full wipe)"
import os
from airflow.utils.session import create_session
from airflow.models.asset import AssetActive, AssetEvent, AssetModel
from sqlalchemy import or_

with create_session() as session:
    q = session.query(AssetModel).filter(
        or_(AssetModel.uri.like(os.environ["URI_A"]), AssetModel.uri.like(os.environ["URI_B"]))
    )
    assets = q.all()
    ids = [a.id for a in assets]
    uris = [a.uri for a in assets]
    if ids:
        session.query(AssetEvent).filter(AssetEvent.asset_id.in_(ids)).delete(synchronize_session=False)
        session.query(AssetActive).filter(AssetActive.uri.in_(uris)).delete(synchronize_session=False)
        session.query(AssetModel).filter(AssetModel.id.in_(ids)).delete(synchronize_session=False)
    print(f"  Removed {len(ids)} Asset row(s).")
PY
}

cmd_reset_watch() {
  # Same as cmd_reset_perfile, for ngt_dags_watch.py's 4 DAGs + its 4 static
  # Assets (ngt://runs, ngt://files/<cal>). Their AssetStateStore rows (the
  # watchers' "seen runs" / "emitted files" watermarks) are removed with the
  # AssetModel via ON DELETE CASCADE.
  activate_venv
  airflow dags delete -y ngt_watch_run_primer
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags delete -y "ngt_watch_process_${lower}"
  done
  _reset_ngt_assets "ngt_dags_watch.py" "ngt://runs%" "ngt://files/%"
  echo "Cleared DAG-run history for all 4 ngt_watch_* DAGs (ngt_dags_watch.py)."
}

cmd_reset_airflow() {
  # Drops and recreates the whole metadata DB schema -- both designs' DAG-run
  # history, variables, connections, everything -- not just this repo's DAGs.
  # Back to the state right after `airflow db migrate` first ran (all DAGs
  # paused by default). Use reset-perfile/reset-watch instead to clear just
  # one design's history and leave the rest of the DB alone.
  #
  # Stops/restarts the 4 tmux-managed processes around the reset (only if
  # they were already running): each one holds in-memory state tied to the
  # pre-reset schema (e.g. the scheduler/dag-processor's own Job row) and
  # reliably crashes on its next DB query once the tables underneath it have
  # been dropped and recreated -- confirmed live (UndefinedTable: relation
  # "job" does not exist) while testing this command. A reset that leaves the
  # instance broken until the user figures out to restart it manually isn't
  # a usable reset.
  local was_running=0
  if tmux has-session -t NGTAirflowScheduler 2>/dev/null; then
    was_running=1
    cmd_stop
  fi

  activate_venv
  echo "Resetting the entire Airflow metadata DB (both DAG designs, variables, connections)..."
  airflow db reset -y
  airflow db migrate
  echo "Airflow metadata DB reset and re-migrated -- all DAGs are paused again, as after a fresh install."

  if [ "$was_running" = "1" ]; then
    echo "Restarting the 4 processes (they were running before the reset)..."
    cmd_start
  fi
}

cmd_reset_scenario() {
  # Wraps scenario-player/sim_env.sh's own sim_reset/sim_setup (wipes and recreates
  # $NGT_DEV_HOME -- every fake OMS run/EOS file/output/log) around a
  # stop/restart of Airflow's processes, for the same reason reset-airflow
  # stops/restarts around its own DB reset: doing this while the triggerer
  # has in-flight deferred work (ngt_dags_per_file.py's wait_for_files)
  # polling paths under $NGT_DEV_HOME left that work stuck in a running/
  # deferred state indefinitely, and each process's own console log silently
  # stopped updating (confirmed live) -- restarting cleanly afterward is what
  # actually recovers them. sim_env.sh's own reset/setup deliberately don't
  # know or check any of this (see their comments) -- that coordination lives
  # here, in the Airflow-specific adapter, on purpose. Doesn't touch DAG-run
  # history -- pair with reset-perfile/reset-watch/reset-airflow for that.
  local was_running=0
  if tmux has-session -t NGTAirflowScheduler 2>/dev/null; then
    was_running=1
    cmd_stop
  fi

  sim_reset
  activate_venv
  sim_setup
  echo "Reset and re-set-up $NGT_DEV_HOME."

  if [ "$was_running" = "1" ]; then
    echo "Restarting the 4 processes (they were running before the reset)..."
    cmd_start
  fi
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  ui) cmd_ui ;;
  logs) cmd_logs "${2:?scheduler, dag-processor, or api required}" ;;
  unpause-perfile) cmd_pause_unpause_perfile unpause ;;
  pause-perfile) cmd_pause_unpause_perfile pause ;;
  unpause-watch) cmd_pause_unpause_watch unpause ;;
  pause-watch) cmd_pause_unpause_watch pause ;;
  reset-perfile) cmd_reset_perfile ;;
  reset-watch) cmd_reset_watch ;;
  reset-airflow) cmd_reset_airflow ;;
  reset-scenario) cmd_reset_scenario ;;
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
  arm-fault)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" arm-fault "$@"
    ;;
  list-faults)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" list-faults "$@"
    ;;
  clear-faults)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" clear-faults "$@"
    ;;
  *) usage; exit 1 ;;
esac
