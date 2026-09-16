#!/bin/bash
# Guided, narrated walkthrough of the per-file DAG design
# (airflow_automation/airflow_dags/ngt_dags_per_file.py, driven via airflow_demo.sh) against
# the fake OMS/EOS/CMSSW toolchain in scenario-player/bin/. Needs a Linux host (Airflow
# has no Windows support; WSL works fine on Windows).
#
# See per_file_scheduling_design.md for a full written explanation of the
# design; watch_interactive_demo.sh is the equivalent walkthrough of the
# AssetWatcher design, with the same structure and narration conventions.
#
# Seeds a run, adds lumisections incrementally, ends the run, and after every
# command shows you the relevant output directories/files and DAG-run history
# so you can watch run_detector -> file_detector (deferred!) -> one process
# DagRun *per lumisection* happening (file_detector reacts as soon as its
# poll_interval next checks, default 5s). Also demonstrates a forced job
# failure to show a real Airflow retry.
#
# This is a scripted narrative, not a reusable driver, so it isn't split into
# separate files the way scenario-player/sim_env.sh/airflow_demo.sh are -- each
# section below is still marked [SIM] (drives the simulation, forwards to
# scenario-player/seed.py, would read the same for any engine) or [AIRFLOW] (inspects
# this specific engine's own state: DAG runs, task states, its UI).
#
# Usage:
#   ./airflow_automation/airflow_demo/perfile_interactive_demo.sh                                   # EcalPedestals, 1 run, 2 LS
#   ./airflow_automation/airflow_demo/perfile_interactive_demo.sh --calibration SiStripBad --ls 4
#   ./airflow_automation/airflow_demo/perfile_interactive_demo.sh --yes                             # don't pause between steps
#
# Options:
#   --calibration NAME   SiStripBad | EcalPedestals | BeamSpot   (default: EcalPedestals)
#   --run N               run number to simulate                  (default: 398700)
#   --ls N                 lumisections to seed (1 immediately, rest added one at a time)  (default: 2)
#   --yes / -y              don't pause for confirmation between steps
#   --keep-running          don't offer to tear down Airflow at the end
#   --skip-failure-demo     skip the forced-failure/retry demonstration at the end

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AIRFLOW_DEMO="$REPO_DIR/airflow_automation/airflow_demo/airflow_demo.sh"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

CALIBRATION="EcalPedestals"
RUN=398700
NUM_LS=2
AUTO_CONTINUE=0
KEEP_RUNNING=0
SKIP_FAILURE_DEMO=0
RUN_DETECTOR_TIMEOUT=45    # ngt_perfile_run_detector ticks every 30s
FILE_TIMEOUT=30            # file_detector's deferred poll_interval is 5s by default -- process
                           # DAG chain (step2->3->4->upload) is a handful of seconds on top of that

while [ $# -gt 0 ]; do
  case "$1" in
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    --ls) NUM_LS="$2"; shift 2 ;;
    --yes|-y) AUTO_CONTINUE=1; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --skip-failure-demo) SKIP_FAILURE_DEMO=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- narration helpers (same conventions as watch_interactive_demo.sh) --------

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
  # show_dag_runs <dag_id> [grep_pattern] -- process is one DAG per
  # calibration (ngt_perfile_process_<calibration>, pass that dag_id
  # directly), but file_detector is still a single generic DAG shared by
  # every calibration/run, so its calls pass a grep pattern to narrow down
  # to the run_ids this walkthrough actually created.
  local dag_id="$1" pattern="${2:-}"
  echo "  Recent DAG runs for $dag_id${pattern:+ matching '$pattern'}:"
  activate_airflow
  if [ -n "$pattern" ]; then
    airflow dags list-runs "$dag_id" 2>/dev/null | grep -i "$pattern" | sed 's/^/    /' || echo "    (none yet)"
  else
    airflow dags list-runs "$dag_id" 2>/dev/null | tail -n +1 | sed 's/^/    /' || echo "    (none yet)"
  fi
}

run() { echo "+ $*"; "$@"; }

lower_calibration() { echo "$CALIBRATION" | tr '[:upper:]' '[:lower:]'; }

# --- 0. sanity --------------------------------------------------------------------------

banner "NGT Calibration Loop -- per-file DAG design live walkthrough"
note "calibration=$CALIBRATION  run=$RUN  ls=$NUM_LS"
note "\$NGT_DEV_HOME=$NGT_DEV_HOME"
note "see per_file_scheduling_design.md for what run_detector/file_detector/process actually do"
if [ ! -f "$HOME/airflow3-ngt-venv/bin/activate" ]; then
  echo "No Airflow venv at ~/airflow3-ngt-venv -- see README.md's Airflow setup section" >&2
  exit 1
fi
command -v tmux >/dev/null || { echo "tmux is required" >&2; exit 1; }
pause

# --- 1. setup ------------------------------------------------------------------------------

banner "[SIM] 1. Setting up the scratch environment"
run "$AIRFLOW_DEMO" setup
show_file "$NGT_DEV_HOME/ngtParameters.jsn" "ngtParameters.jsn (paths redirected into \$NGT_DEV_HOME)"
pause

# --- 2. start airflow (incl. triggerer) + unpause the per-file DAGs -------------------------

banner "[AIRFLOW] 2. Starting Airflow (scheduler + dag-processor + API server + triggerer) and unpausing the 5 per-file DAGs"
run "$AIRFLOW_DEMO" start
note "giving the dag-processor time to parse airflow_automation/airflow_dags/ngt_dags_per_file.py..."
sleep 15
note "this is one unpause call, not one per calibration --"
note "ngt_perfile_file_detector is one generic DAG shared by every calibration; process is one per calibration"
run "$AIRFLOW_DEMO" unpause-perfile
run "$AIRFLOW_DEMO" status
run "$AIRFLOW_DEMO" ui
note "open that URL any time to watch DAG runs graphically instead of via this script"
pause

RUN_DATA_DIR="$NGT_DEV_HOME/data/$CALIBRATION"
RUN_DIR="$RUN_DATA_DIR/run$RUN"
FIRST_LS=51
LOWER="$(lower_calibration)"

# --- 3. seed a live run and watch run_detector -> file_detector pick it up -------------------

banner "[SIM+AIRFLOW] 3. Seeding run $RUN with its first lumisection ($FIRST_LS) and watching run_detector latch it"
run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$RUN" --ls "$FIRST_LS"
run "$AIRFLOW_DEMO" list-runs
note "ngt_perfile_run_detector's detect_${LOWER} task reuses step2.find_new_run unchanged --"
note "the run's working dir appears the moment it latches"
wait_until_glob "$RUN_DATA_DIR" "run${RUN}" "$RUN_DETECTOR_TIMEOUT" "run_detector to latch run $RUN (creates its working dir)"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs ngt_perfile_file_detector "${LOWER}_run${RUN}"
pause

banner "[SIM] 3.a  Waiting (deferred!) for file_detector to notice LS $FIRST_LS and dispatch a processing run for it"
note "wait_for_files is sitting DEFERRED right now -- not polling, not occupying a worker slot --"
note "check the Airflow UI's Grid view for ngt_perfile_file_detector: the task shows as 'deferred'"
wait_until_glob "$RUN_DIR" "run${RUN}_LS*.root" "$FILE_TIMEOUT" "Step 2's output for LS $FIRST_LS"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs "ngt_perfile_process_${LOWER}" "run${RUN}"
pause

banner "[SIM] 3.b  Waiting for that SAME processing DagRun to also finish Step 3 and Step 4 (one DagRun, all three steps)"
wait_until_glob "$RUN_DIR" "alcaPromptJob*" "$FILE_TIMEOUT" "an alcaPromptJob_* directory (Step 3, this file only)"
wait_until_glob "$RUN_DIR" "harvestJob*" "$FILE_TIMEOUT" "a harvestJob_* directory (Step 4, every file accumulated so far)"
show_tree "$RUN_DIR" "Run $RUN working dir"
alca_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'alcaPromptJob*' 2>/dev/null | sort | tail -1)"
[ -n "$alca_dir" ] && show_file "$alca_dir/stdout.log" "Latest ALCA job stdout (fake cmsDriver.py + cmsRun output)"
harvest_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'harvestJob*' 2>/dev/null | sort | tail -1)"
if [ -n "$harvest_dir" ]; then
  show_file "$harvest_dir/upload_stdout.log" "Latest condDB upload stdout (fake uploadConditions.py, separate retryable task)"
fi
pause

if [ "$NUM_LS" -gt 1 ]; then
  for extra in $(seq 2 "$NUM_LS"); do
    LS=$((FIRST_LS + extra - 1))
    banner "[SIM] 3.c$extra  Simulating a new lumisection arriving: run $RUN, LS $LS"
    note "one more full step2->3->4 processing DagRun will run for just this file --"
    note "Step 4 will re-harvest EVERY file accumulated for this run so far, not just this one"
    run "$AIRFLOW_DEMO" add-ls "$CALIBRATION" "$RUN" "$LS"
    wait_until_glob "$RUN_DIR" "run${RUN}_LS*${LS}*.root" "$FILE_TIMEOUT" "Step 2's output for LS $LS"
    show_tree "$RUN_DIR" "Run $RUN working dir"
    pause
  done
fi

show_dag_runs "ngt_perfile_process_${LOWER}" "run${RUN}"
note "one process DagRun per lumisection above; harvestJob_* directories may be fewer than that --"
note "concurrent per-file DagRuns can converge on the same accumulated-file-set hash and share a dir"
pause

# --- 4. end the run and watch file_detector conclude -----------------------------------------

banner "[SIM] 4. Ending run $RUN and watching file_detector's final cycle"
run "$AIRFLOW_DEMO" end-run "$RUN"
note "file_detector will notice the run ended (once run_detector's own OMS view agrees), touch"
note "runEnd.log, and stop retriggering itself -- no separate Step 2/3/4 finalization DAGs needed"
wait_until_glob "$RUN_DIR" "runEnd.log" "$((RUN_DETECTOR_TIMEOUT + FILE_TIMEOUT))" "file_detector to write runEnd.log"
show_file "$RUN_DIR/runEnd.log" "runEnd.log"
show_file "$RUN_DIR/allLSProcessed.log" "allLSProcessed.log"
show_dag_runs ngt_perfile_file_detector "${LOWER}_run${RUN}"
pause

# --- 5. forced-failure / retry demonstration --------------------------------------------------

if [ "$SKIP_FAILURE_DEMO" != "1" ]; then
  banner "[SIM+AIRFLOW] 5. Demonstrating a real Airflow retry (step2_express)"
  note "Every processing-DAG task that launches a job (step2_express/step3_alca/step4_harvest)"
  note "blocks and is retried by Airflow (3 retries, exponential backoff)."
  if confirm "Temporarily break the fake cmsRun to force a failure and watch step2_express retry?"; then
    FAILURE_RUN=$((RUN + 1))
    run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$FAILURE_RUN" --ls 51
    mv "$NGT_DEV_HOME/bin/cmsRun" "$NGT_DEV_HOME/bin/cmsRun.disabled"
    note "cmsRun disabled -- watching the next step2_express task fail and retry..."
    wait_until_glob "$NGT_DEV_HOME/logs" "does-not-exist" 20 "(just pausing for a retry cycle)"
    activate_airflow
    echo "  Recent task instances for ngt_perfile_process_${LOWER}/step2_express (run $FAILURE_RUN):"
    failure_run_id="$(airflow dags list-runs "ngt_perfile_process_${LOWER}" -o plain 2>/dev/null | grep "run${FAILURE_RUN}__" | head -1 | awk '{print $2}')"
    if [ -n "$failure_run_id" ]; then
      airflow tasks states-for-dag-run "ngt_perfile_process_${LOWER}" "$failure_run_id" 2>/dev/null | sed 's/^/    /'
    else
      note "(couldn't find that run's processing DagRun yet -- open the Airflow UI's Grid view instead: $AIRFLOW_DEMO ui)"
    fi
    note "check the Airflow UI (Grid view, ngt_perfile_process_${LOWER}) for the up_for_retry / failed -> retried task state"
    pause
    mv "$NGT_DEV_HOME/bin/cmsRun.disabled" "$NGT_DEV_HOME/bin/cmsRun"
    note "cmsRun restored -- the next retry attempt should succeed"
    pause
  fi
fi

# --- 6. summary ---------------------------------------------------------------------------

banner "6. Summary"
show_tree "$RUN_DATA_DIR" "Everything produced for $CALIBRATION"
run "$AIRFLOW_DEMO" status
run "$AIRFLOW_DEMO" ui

if [ "$KEEP_RUNNING" = "1" ]; then
  echo; echo "Leaving Airflow running (--keep-running). Stop later with: $AIRFLOW_DEMO stop"; exit 0
fi
if [ "$AUTO_CONTINUE" = "1" ]; then
  echo; echo "(--yes: leaving Airflow running. Stop with: $AIRFLOW_DEMO stop)"; exit 0
fi

echo
if confirm "Pause the per-file DAGs again (leaving the shared Airflow instance running)?"; then
  run "$AIRFLOW_DEMO" pause-perfile
fi
if confirm "Tear down Airflow entirely now?"; then
  run "$AIRFLOW_DEMO" stop
else
  echo "Left running. Stop later with: $AIRFLOW_DEMO stop"
fi
