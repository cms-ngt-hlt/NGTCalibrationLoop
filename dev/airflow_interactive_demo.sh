#!/bin/bash
# Guided, narrated walkthrough of the Airflow-based NGTCalibrationLoop pipeline
# (airflow_dags/ngt_dags.py, driven via dev/airflow_demo.sh) against the fake
# OMS/EOS/CMSSW toolchain in dev/bin/. Needs a Linux host (Airflow has no
# Windows support; WSL works fine on Windows).
#
# Seeds a run, adds lumisections incrementally, ends the run, and after every
# command shows you the relevant output directories/files and DAG-run history
# so you can actually see the latch -> process(xN cycles) -> handoff chain
# happening, pausing for confirmation between steps so you have time to look
# before moving on. Also demonstrates a forced job failure to show a real
# Airflow retry.
#
# Usage:
#   ./dev/airflow_interactive_demo.sh                                   # EcalPedestals, 1 run, 2 LS
#   ./dev/airflow_interactive_demo.sh --calibration SiStripBad --ls 4
#   ./dev/airflow_interactive_demo.sh --yes                             # don't pause between steps
#
# Options:
#   --calibration NAME   SiStripBad | EcalPedestals | BeamSpot   (default: EcalPedestals)
#   --run N               run number to simulate                  (default: 398600)
#   --ls N                 lumisections to seed (1 immediately, rest added one at a time)  (default: 2)
#   --yes / -y              don't pause for confirmation between steps
#   --keep-running          don't offer to tear down Airflow at the end
#   --skip-failure-demo     skip the forced-failure/retry demonstration at the end

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIRFLOW_DEMO="$REPO_DIR/dev/airflow_demo.sh"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

CALIBRATION="EcalPedestals"
RUN=398600
NUM_LS=2
AUTO_CONTINUE=0
KEEP_RUNNING=0
SKIP_FAILURE_DEMO=0
LATCH_TIMEOUT=45     # latch DAGs tick every 30s
CYCLE_TIMEOUT=60     # a process-DAG cycle chain: check -> prepare -> launch -> finalize -> advance

while [ $# -gt 0 ]; do
  case "$1" in
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    --ls) NUM_LS="$2"; shift 2 ;;
    --yes|-y) AUTO_CONTINUE=1; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    --skip-failure-demo) SKIP_FAILURE_DEMO=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- narration helpers (same conventions as dev/interactive_demo.sh) ----------------

banner() { echo; echo "================================================================================"; echo "  $*"; echo "================================================================================"; }
note() { echo "  -- $*"; }

pause() {
  if [ "$AUTO_CONTINUE" = "1" ]; then echo "(--yes: auto-continuing)"; return 0; fi
  echo
  read -rp ">>> Press Enter to continue (Ctrl-C to stop here)... " _
}

confirm() {
  # confirm <prompt> -- returns 0 (yes) if AUTO_CONTINUE or the user says y
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
    sleep 3; waited=$((waited + 3)); echo -n "."
  done
  echo
  if [ -n "$found" ]; then
    echo "  -> appeared after ~${waited}s: $found"
  else
    echo "  -> still nothing matching '$pattern' under $dir after ${timeout}s"
    echo "     (check: $0's --calibration matches, or: $AIRFLOW_DEMO ui)"
  fi
}

show_dag_runs() {
  # show_dag_runs <dag_id_suffix>, e.g. "step2_ecalpedestals_process"
  local lower; lower="$(echo "$CALIBRATION" | tr '[:upper:]' '[:lower:]')"
  local dag_id="ngt_${1//__CAL__/$lower}"
  echo "  Recent DAG runs for $dag_id:"
  source "$HOME/airflow-ngt-venv/bin/activate" 2>/dev/null
  source "$REPO_DIR/dev/airflow_env.sh" 2>/dev/null
  airflow dags list-runs -d "$dag_id" 2>/dev/null | tail -n +1 | sed 's/^/    /' || echo "    (none yet)"
}

run() { echo "+ $*"; "$@"; }

# --- 0. sanity --------------------------------------------------------------------------

banner "NGT Calibration Loop -- Airflow live walkthrough"
note "calibration=$CALIBRATION  run=$RUN  ls=$NUM_LS"
note "\$NGT_DEV_HOME=$NGT_DEV_HOME"
if [ ! -f "$HOME/airflow-ngt-venv/bin/activate" ]; then
  echo "No Airflow venv at ~/airflow-ngt-venv -- see README.md's Airflow setup section" >&2
  exit 1
fi
command -v tmux >/dev/null || { echo "tmux is required" >&2; exit 1; }
pause

# --- 1. setup ------------------------------------------------------------------------------

banner "1. Setting up the scratch environment"
run "$AIRFLOW_DEMO" setup
show_file "$NGT_DEV_HOME/ngtParameters.jsn" "ngtParameters.jsn (paths redirected into \$NGT_DEV_HOME)"
pause

# --- 2. start airflow + unpause this calibration's DAGs -------------------------------------

banner "2. Starting Airflow (webserver + scheduler) and unpausing $CALIBRATION's 6 DAGs"
run "$AIRFLOW_DEMO" start
note "giving the scheduler time to parse airflow_dags/ng_dags.py..."
sleep 15
run "$AIRFLOW_DEMO" unpause "$CALIBRATION"
run "$AIRFLOW_DEMO" status
run "$AIRFLOW_DEMO" ui
note "open that URL any time to watch DAG runs graphically instead of via this script"
pause

RUN_DATA_DIR="$NGT_DEV_HOME/data/$CALIBRATION"
RUN_DIR="$RUN_DATA_DIR/run$RUN"
FIRST_LS=51

# --- 3. seed a live run and watch the latch DAG pick it up -----------------------------------

banner "3. Seeding run $RUN with its first lumisection ($FIRST_LS) and watching the latch DAG"
run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$RUN" --ls "$FIRST_LS"
run "$AIRFLOW_DEMO" list-runs
wait_until_glob "$RUN_DATA_DIR" "run${RUN}" "$LATCH_TIMEOUT" "ngt_step2_${CALIBRATION,,}_latch to latch run $RUN (creates its working dir)"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs "step2___CAL___latch"
pause

banner "3.a  Waiting for the Step 2 processor DAG to launch an express job for LS $FIRST_LS"
wait_until_glob "$RUN_DIR" "run${RUN}_LS*.root" "$CYCLE_TIMEOUT" "Step 2's express job output (*.root)"
show_tree "$RUN_DIR" "Run $RUN working dir"
show_dag_runs "step2___CAL___process"
pause

if [ "$NUM_LS" -gt 1 ]; then
  for extra in $(seq 2 "$NUM_LS"); do
    LS=$((FIRST_LS + extra - 1))
    banner "3.b$extra  Simulating a new lumisection arriving: run $RUN, LS $LS"
    run "$AIRFLOW_DEMO" add-ls "$CALIBRATION" "$RUN" "$LS"
    wait_until_glob "$RUN_DIR" "run${RUN}_LS*.root" "$CYCLE_TIMEOUT" "the next Step 2 cycle to pick it up"
    show_tree "$RUN_DIR" "Run $RUN working dir"
    pause
  done
fi

# --- 4. step3/4 handoff ---------------------------------------------------------------------

banner "4. Waiting for the Step 3 latch DAG to notice the run dir and hand off to its processor"
wait_until_glob "$RUN_DIR" "alcaPromptJob*" "$((LATCH_TIMEOUT + CYCLE_TIMEOUT))" "an alcaPromptJob_* directory"
show_tree "$RUN_DIR" "Run $RUN working dir"
alca_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'alcaPromptJob*' 2>/dev/null | sort | tail -1)"
[ -n "$alca_dir" ] && show_file "$alca_dir/stdout.log" "Latest ALCA job stdout (fake cmsDriver.py + cmsRun output)"
show_dag_runs "step3___CAL___process"
pause

banner "4.a  Waiting for Step 4's latch + processor to harvest the ALCARECO output"
wait_until_glob "$RUN_DIR" "harvestJob*" "$((LATCH_TIMEOUT + CYCLE_TIMEOUT))" "a harvestJob_* directory"
show_tree "$RUN_DIR" "Run $RUN working dir"
harvest_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'harvestJob*' 2>/dev/null | sort | tail -1)"
if [ -n "$harvest_dir" ]; then
  show_file "$harvest_dir/stdout.log" "Latest harvest job stdout (fake cmsRun)"
  show_file "$harvest_dir/upload_stdout.log" "Latest condDB upload stdout (fake uploadConditions.py, separate retryable task)"
fi
show_dag_runs "step4___CAL___process"
pause

# --- 5. end the run and watch finalization cascade -------------------------------------------

banner "5. Ending run $RUN and watching the finalization cascade through Step 2 -> 3 -> 4"
run "$AIRFLOW_DEMO" end-run "$RUN"
wait_until_glob "$RUN_DIR" "runEnd.log" "$CYCLE_TIMEOUT" "Step 2 to finalize the run"
show_file "$RUN_DIR/runEnd.log" "runEnd.log"
show_file "$RUN_DIR/allLSProcessed.log" "allLSProcessed.log"
wait_until_glob "$RUN_DIR" "allStep2FilesProcessed.log" "$CYCLE_TIMEOUT" "Step 3's final cycle"
show_file "$RUN_DIR/allStep2FilesProcessed.log" "allStep2FilesProcessed.log (Step 3's summary)"
wait_until_glob "$RUN_DIR" "allStep3FilesProcessed.log" "$CYCLE_TIMEOUT" "Step 4's final cycle"
show_file "$RUN_DIR/allStep3FilesProcessed.log" "allStep3FilesProcessed.log (Step 4's summary)"
pause

# --- 6. forced-failure / retry demonstration --------------------------------------------------

if [ "$SKIP_FAILURE_DEMO" != "1" ]; then
  banner "6. Demonstrating a real Airflow retry (the whole point of this migration)"
  note "The old FSM launched jobs with subprocess.Popen and never checked the exit code --"
  note "a failure would vanish silently. Now launch_job blocks and Airflow retries it."
  if confirm "Temporarily break the fake cmsRun to force a failure and watch launch_job retry?"; then
    run "$AIRFLOW_DEMO" seed-run "$CALIBRATION" "$((RUN + 1))" --ls 51
    mv "$NGT_DEV_HOME/bin/cmsRun" "$NGT_DEV_HOME/bin/cmsRun.disabled"
    note "cmsRun disabled -- watching the next Step 2 launch_job task fail and retry..."
    wait_until_glob "$NGT_DEV_HOME/logs" "does-not-exist" 20 "(just pausing for a retry cycle)"
    source "$HOME/airflow-ngt-venv/bin/activate"; source "$REPO_DIR/dev/airflow_env.sh"
    echo "  Recent task instances for ngt_step2_${CALIBRATION,,}_process/launch_job:"
    airflow tasks states-for-dag-run "ngt_step2_${CALIBRATION,,}_process" "$(airflow dags list-runs -d "ngt_step2_${CALIBRATION,,}_process" -o plain 2>/dev/null | tail -1 | awk '{print $2}')" 2>/dev/null | sed 's/^/    /' || note "(open the Airflow UI's Grid view instead: $AIRFLOW_DEMO ui)"
    note "check the Airflow UI (Grid view) for the up_for_retry / failed -> retried task state"
    pause
    mv "$NGT_DEV_HOME/bin/cmsRun.disabled" "$NGT_DEV_HOME/bin/cmsRun"
    note "cmsRun restored -- the next retry attempt should succeed"
    pause
  fi
fi

# --- 7. summary ---------------------------------------------------------------------------

banner "7. Summary"
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
if confirm "Tear down Airflow now?"; then
  run "$AIRFLOW_DEMO" stop
else
  echo "Left running. Stop later with: $AIRFLOW_DEMO stop"
fi
