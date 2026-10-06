#!/bin/bash
# Guided, narrated walkthrough of the AssetWatcher DAG design -- the all-static-
# asset, AssetWatcher-driven variant (airflow_automation/airflow_dags/ngt_dags_watch.py, driven
# via airflow_demo.sh) against the fake OMS/EOS/CMSSW toolchain in
# scenario-player/bin/. Needs a Linux host (Airflow has no Windows support; WSL works).
#
# Narration conventions shared with the other demo scripts. What is
# different to *watch for* here:
#   * NO cron DAGs at all. Two kinds of AssetWatcher run continuously in the
#     `airflow triggerer` (while ngt_watch_* is unpaused):
#       - RunWatcherTrigger polls OMS and updates the static Asset ngt://runs;
#       - LumisectionFileWatcherTrigger x3 watch each calibration's active run
#         dir and update the static Asset ngt://files/<cal>, one event per file.
#   * ngt_watch_run_primer (schedule=[Asset ngt://runs]) reacts to a new run by
#     calling step2.find_new_run per calibration -- creating the working dirs
#     the file watchers gate on. That's what "starts" per-calibration file
#     watching (the watchers are always running, just idle until a dir exists).
#   * one ngt_watch_process_<cal> DagRun per lumisection.
#   * all 4 assets are static, so they ALL show in the UI's Assets view; the
#     watchers' dedup watermarks live in each Asset's AssetStateStore.
# See watch_asset_scheduling_design.md for the full written explanation.
#
# Usage:
#   ./airflow_automation/airflow_demo/watch_interactive_demo.sh                                   # EcalPedestals, 1 run, 2 LS
#   ./airflow_automation/airflow_demo/watch_interactive_demo.sh --calibration SiStripBad --ls 4
#   ./airflow_automation/airflow_demo/watch_interactive_demo.sh --yes                             # don't pause between steps
#
# Options:
#   --calibration NAME   SiStripBad | EcalPedestals | BeamSpot   (default: EcalPedestals)
#   --run N               run number to simulate                  (default: 399100)
#   --ls N                 lumisections to seed (1 immediately, rest added one at a time)  (default: 2)
#   --yes / -y              don't pause for confirmation between steps
#   --keep-running          don't offer to tear down Airflow at the end
#   --skip-failure-demo     skip the forced-failure/retry demonstration at the end

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AIRFLOW_DEMO="$REPO_DIR/airflow_automation/airflow_demo/airflow_demo.sh"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

CALIBRATION="EcalPedestals"
RUN=399100
NUM_LS=2
AUTO_CONTINUE=0
KEEP_RUNNING=0
SKIP_FAILURE_DEMO=0
RUN_TIMEOUT=45     # RunWatcherTrigger poll_interval is 5s by default; primer + dir creation a few s
FILE_TIMEOUT=30   # LumisectionFileWatcherTrigger poll_interval 5s + process DAG chain a few s

while [ $# -gt 0 ]; do
  case "$1" in
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    --ls) NUM_LS="$2"; shift 2 ;;
    --yes|-y) AUTO_CONTINUE=1; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --skip-failure-demo) SKIP_FAILURE_DEMO=1; shift ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- narration helpers --------------------------------------------------------

banner() { echo; echo "================================================================================"; echo "  $*"; echo "================================================================================"; }
note() { echo "  -- $*"; }

pause() {
  if [ "$AUTO_CONTINUE" = "1" ]; then echo "(--yes: auto-continuing)"; return 0; fi
  echo
  read -rp ">>> Press Enter to continue (Ctrl-C to stop here)... " _
}

confirm() {
  local prompt="$1"
  if [ "$AUTO_CONTINUE" = "1" ]; then return 0; fi
  local reply
  read -rp ">>> $prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]]
}

show_tree() {
  local dir="$1" label="$2"
  echo "  $label ($dir):"
  if [ -d "$dir" ] && [ -n "$(find "$dir" -maxdepth 4 -mindepth 1 2>/dev/null)" ]; then
    find "$dir" -maxdepth 4 2>/dev/null | sort | sed "s|^$dir|   .|"
  else
    echo "    (empty or not created yet)"
  fi
}

show_file() {
  local path="$1" label="$2" max_lines="${3:-15}"
  echo "  $label ($path):"
  if [ -f "$path" ]; then sed 's/^/    | /' "$path" | tail -n "$max_lines"; else echo "    (not present)"; fi
}

wait_until_glob() {
  local dir="$1" pattern="$2" timeout="$3" description="$4"
  echo -n "  Waiting up to ${timeout}s for $description "
  local waited=0 found=""
  while [ "$waited" -lt "$timeout" ]; do
    found="$(find "$dir" -maxdepth 4 -name "$pattern" 2>/dev/null | sort | head -1)"
    [ -n "$found" ] && break
    sleep 2; waited=$((waited + 2)); echo -n "."
  done
  echo
  if [ -n "$found" ]; then
    echo "  -> appeared after ~${waited}s: $found"
  else
    echo "  -> still nothing matching '$pattern' under $dir after ${timeout}s"
    echo "     (check: $0's --calibration matches, or: $AIRFLOW_DEMO ui)"
  fi
}

activate_airflow() {
  source "$HOME/airflow3-ngt-venv/bin/activate" 2>/dev/null
  source "$REPO_DIR/airflow_automation/airflow_demo/airflow_env.sh" 2>/dev/null
}

show_dag_runs() {
  local dag_id="$1" pattern="${2:-}"
  echo "  Recent DAG runs for $dag_id${pattern:+ matching '$pattern'}:"
  activate_airflow
  if [ -n "$pattern" ]; then
    airflow dags list-runs "$dag_id" 2>/dev/null | grep -i "$pattern" | sed 's/^/    /' || echo "    (none yet)"
  else
    airflow dags list-runs "$dag_id" 2>/dev/null | tail -n +1 | sed 's/^/    /' || echo "    (none yet)"
  fi
}

show_assets() {
  activate_airflow
  echo "  Static Assets (all show in the UI Assets view -- $AIRFLOW_DEMO ui):"
  airflow assets list 2>/dev/null | grep -E 'ngt://runs|ngt://files/' | sed 's/^/    /' \
    || echo "    (not registered yet -- unpause ngt_watch_* first)"
  note "watermarks (seen runs / emitted files) are in each Asset's AssetStateStore:"
  note "  Airflow UI -> Assets -> <asset> -> State store, or GET /api/v2/assets/{id}/state-store"
}

show_triggerer_watchers() {
  local log="$NGT_DEV_HOME/logs/tmux_console_airflow_triggerer.log"
  echo "  Triggerer watcher activity (last few lines of $log):"
  if [ -f "$log" ]; then
    grep -iE 'watch|RunWatcher|LumisectionFileWatcher|ngt://' "$log" | tail -n 8 | sed 's/^/    /' || echo "    (no watcher lines yet)"
  else
    echo "    (no triggerer log yet -- has '$AIRFLOW_DEMO start' been run?)"
  fi
}

run() { echo "+ $*"; "$@"; }
lower_calibration() { echo "$CALIBRATION" | tr '[:upper:]' '[:lower:]'; }

# --- 0. sanity ------------------------------------------------------------------------

banner "NGT Calibration Loop -- AssetWatcher (all-static-asset) DAG design live walkthrough"
note "calibration=$CALIBRATION  run=$RUN  ls=$NUM_LS"
note "\$NGT_DEV_HOME=$NGT_DEV_HOME"
note "see watch_asset_scheduling_design.md for what run_primer / the watchers / process actually do"
if [ ! -f "$HOME/airflow3-ngt-venv/bin/activate" ]; then
  echo "No Airflow venv at ~/airflow3-ngt-venv -- see README.md's Airflow setup section" >&2
  exit 1
fi
command -v tmux >/dev/null || { echo "tmux is required" >&2; exit 1; }
pause

# --- 1. setup -----------------------------------------------------------------------------

banner "[SIM] 1. Setting up the scratch environment"
run "$AIRFLOW_DEMO" setup
show_file "$NGT_DEV_HOME/ngtParameters.jsn" "ngtParameters.jsn (paths redirected into \$NGT_DEV_HOME)"
pause

# --- 2. start airflow + unpause ONLY the watch DAGs -------------------------------------

banner "[AIRFLOW] 2. Starting Airflow (incl. triggerer -- it hosts the watchers) and unpausing the 4 watch DAGs"
run "$AIRFLOW_DEMO" start
note "giving the dag-processor time to parse airflow_automation/airflow_dags/ngt_dags_watch.py..."
sleep 15
note "unpausing ngt_watch_* is also what STARTS the 4 AssetWatchers in the triggerer"
run "$AIRFLOW_DEMO" unpause-watch
run "$AIRFLOW_DEMO" status
run "$AIRFLOW_DEMO" ui
show_assets
pause

RUN_DATA_DIR="$NGT_DEV_HOME/data/$CALIBRATION"
RUN_DIR="$RUN_DATA_DIR/run$RUN"
FIRST_LS=51
LOWER="$(lower_calibration)"

# --- 3. seed a run and watch RunWatcherTrigger -> primer -> file watchers -----------------

banner "[SIM+AIRFLOW] 3. Seeding run $RUN with its first lumisection ($FIRST_LS)"
run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$RUN" --ls "$FIRST_LS"
run "$AIRFLOW_DEMO" list-runs
note "RunWatcherTrigger (in the triggerer) polls OMS, sees run $RUN, updates Asset ngt://runs;"
note "that wakes ngt_watch_run_primer, which calls step2.find_new_run per calibration -> working dirs"
wait_until_glob "$RUN_DATA_DIR" "run${RUN}" "$RUN_TIMEOUT" "run_primer to create run $RUN's working dir"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs ngt_watch_run_primer
show_triggerer_watchers
pause

banner "[SIM] 3.a  Watching LumisectionFileWatcherTrigger notice LS $FIRST_LS and fire ngt://files/${LOWER}"
note "the file watcher was idle (no run dir); now it scans run$RUN/, sees the new file, and yields"
note "one Asset event -> one ngt_watch_process_${LOWER} DagRun for that file"
wait_until_glob "$RUN_DIR" "run${RUN}_LS*.root" "$FILE_TIMEOUT" "Step 2's output for LS $FIRST_LS"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs "ngt_watch_process_${LOWER}" "run${RUN}"
show_assets
pause

banner "[SIM] 3.b  Same processing DagRun finishing Step 3 and Step 4"
wait_until_glob "$RUN_DIR" "alcaPromptJob*" "$FILE_TIMEOUT" "an alcaPromptJob_* directory (Step 3)"
wait_until_glob "$RUN_DIR" "harvestJob*" "$FILE_TIMEOUT" "a harvestJob_* directory (Step 4)"
show_tree "$RUN_DIR" "Run $RUN working dir"
pause

if [ "$NUM_LS" -gt 1 ]; then
  for extra in $(seq 2 "$NUM_LS"); do
    LS=$((FIRST_LS + extra - 1))
    banner "[SIM] 3.c$extra  New lumisection: run $RUN, LS $LS  (one more process DagRun for just this file)"
    run "$AIRFLOW_DEMO" add-ls "$CALIBRATION" "$RUN" "$LS"
    wait_until_glob "$RUN_DIR" "run${RUN}_LS*${LS}*.root" "$FILE_TIMEOUT" "Step 2's output for LS $LS"
    show_tree "$RUN_DIR" "Run $RUN working dir"
    pause
  done
fi

show_dag_runs "ngt_watch_process_${LOWER}" "run${RUN}"
note "one process DagRun per lumisection above"
pause

# --- 4. end the run and watch the file watcher write runEnd.log and go idle ---------------

banner "[SIM] 4. Ending run $RUN"
run "$AIRFLOW_DEMO" end-run "$RUN"
note "LumisectionFileWatcherTrigger notices (check_ls_for_processing final, or the OMS run-end grace),"
note "writes runEnd.log via step2.finalize_cycle, drops its emitted-files watermark, and goes idle"
wait_until_glob "$RUN_DIR" "runEnd.log" "$((RUN_TIMEOUT + FILE_TIMEOUT))" "the file watcher to write runEnd.log"
show_file "$RUN_DIR/runEnd.log" "runEnd.log"
show_file "$RUN_DIR/allLSProcessed.log" "allLSProcessed.log"
show_triggerer_watchers
note "the SiStripBad/BeamSpot file watchers also wrote runEnd.log for run $RUN -- nothing to process"
note "(no EOS files were seeded for them), same 'calibration that never gets files' behaviour as #2/#3"
pause

# --- 5. forced-failure / retry demonstration --------------------------------------------------

if [ "$SKIP_FAILURE_DEMO" != "1" ]; then
  banner "[SIM+AIRFLOW] 5. A real Airflow retry (step2_express -- shared _process_tasks.py callable)"
  note "the step2/3/4 callables live in _process_tasks.py -- this design changes only how the process DagRun"
  note "gets triggered (an Asset event from a watcher), not the retry behaviour."
  if confirm "Temporarily break the fake cmsRun to force a failure and watch step2_express retry?"; then
    FAILURE_RUN=$((RUN + 1))
    run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$FAILURE_RUN" --ls 51
    mv "$NGT_DEV_HOME/bin/cmsRun" "$NGT_DEV_HOME/bin/cmsRun.disabled"
    note "cmsRun disabled -- watching the next step2_express task fail and retry..."
    wait_until_glob "$NGT_DEV_HOME/logs" "does-not-exist" 20 "(just pausing for a retry cycle)"
    activate_airflow
    failure_run_id="$(airflow dags list-runs "ngt_watch_process_${LOWER}" -o plain 2>/dev/null | grep "run${FAILURE_RUN}" | head -1 | awk '{print $2}')"
    if [ -n "$failure_run_id" ]; then
      airflow tasks states-for-dag-run "ngt_watch_process_${LOWER}" "$failure_run_id" 2>/dev/null | sed 's/^/    /'
    else
      note "(couldn't find that run's processing DagRun yet -- open the Airflow UI's Grid view: $AIRFLOW_DEMO ui)"
    fi
    pause
    mv "$NGT_DEV_HOME/bin/cmsRun.disabled" "$NGT_DEV_HOME/bin/cmsRun"
    note "cmsRun restored -- the next retry attempt should succeed"
    pause
  fi
fi

# --- 6. summary -----------------------------------------------------------------------------

banner "6. Summary"
show_tree "$RUN_DATA_DIR" "Everything produced for $CALIBRATION"
show_assets
run "$AIRFLOW_DEMO" status
run "$AIRFLOW_DEMO" ui

if [ "$KEEP_RUNNING" = "1" ]; then
  echo; echo "Leaving Airflow running (--keep-running). Stop later with: $AIRFLOW_DEMO stop"; exit 0
fi
if [ "$AUTO_CONTINUE" = "1" ]; then
  echo; echo "(--yes: pausing the watch DAGs -- this stops the watchers -- and leaving Airflow up)"
  run "$AIRFLOW_DEMO" pause-watch
  exit 0
fi

echo
if confirm "Pause the watch DAGs again (this stops the 4 AssetWatchers)?"; then
  run "$AIRFLOW_DEMO" pause-watch
fi
if confirm "Tear down Airflow entirely now?"; then
  run "$AIRFLOW_DEMO" stop
else
  echo "Left running. Stop later with: $AIRFLOW_DEMO stop"
fi
