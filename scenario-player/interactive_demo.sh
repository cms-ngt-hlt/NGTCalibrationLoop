#!/bin/bash
# Guided, narrated walkthrough of the whole NGTCalibrationLoop pipeline running
# live (via scenario-player/live_demo.sh) against the fake OMS/EOS/CMSSW toolchain in scenario-player/bin/.
#
# Seeds a couple of runs, each with a few lumisections (first a couple seeded
# immediately, then more added incrementally to mimic real data arriving), and
# after every command shows you the relevant output directories/files so you can
# see the state machines actually doing something -- run latching, express job
# output, ALCARECO batching, harvesting, and run finalization.
#
# Usage:
#   ./scenario-player/interactive_demo.sh                                   # defaults: EcalPedestals, 2 runs, 2 LS each
#   ./scenario-player/interactive_demo.sh --calibration SiStripBad --runs 3 --ls-per-run 4
#   ./scenario-player/interactive_demo.sh --yes                             # don't pause between steps (e.g. for a dry run)
#
# Options:
#   --calibration NAME   SiStripBad | EcalPedestals | BeamSpot   (default: EcalPedestals)
#   --runs N              number of runs to simulate              (default: 2)
#   --ls-per-run N        lumisections per run                    (default: 2)
#   --base-run N           first run number; subsequent runs are base+1, base+2, ...  (default: 398600)
#   --yes / -y             don't pause for confirmation between steps
#   --keep-running         don't offer to tear down tmux sessions at the end
#
# $NGT_DEV_HOME (default ./demo-env inside this repo, gitignored) and
# NGT_LOOP_SLEEP_SECONDS (Step 3/4 poll interval) can be overridden via
# environment variables -- see scenario-player/live_demo.sh.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_DEMO="$REPO_DIR/scenario-player/live_demo.sh"
export NGT_DEV_HOME="${NGT_DEV_HOME:-$REPO_DIR/demo-env}"

CALIBRATION="EcalPedestals"
NUM_RUNS=2
LS_PER_RUN=2
BASE_RUN=398600
AUTO_CONTINUE=0
KEEP_RUNNING=0
STEP2_WAIT_TIMEOUT=7   # Step2 polls every 1s -- fast
STEP34_WAIT_TIMEOUT=15  # Step3/4 poll every NGT_LOOP_SLEEP_SECONDS (5s) across several sub-states per cycle

while [ $# -gt 0 ]; do
  case "$1" in
    --calibration) CALIBRATION="$2"; shift 2 ;;
    --runs) NUM_RUNS="$2"; shift 2 ;;
    --ls-per-run) LS_PER_RUN="$2"; shift 2 ;;
    --base-run) BASE_RUN="$2"; shift 2 ;;
    --yes|-y) AUTO_CONTINUE=1; shift ;;
    --keep-running) KEEP_RUNNING=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- narration helpers --------------------------------------------------------------

banner() {
  echo
  echo "================================================================================"
  echo "  $*"
  echo "================================================================================"
}

note() {
  echo "  -- $*"
}

pause() {
  if [ "$AUTO_CONTINUE" = "1" ]; then
    echo "(--yes: auto-continuing)"
    return 0
  fi
  echo
  read -rp ">>> Press Enter to continue (Ctrl-C to stop here)... " _
}

show_tree() {
  # show_tree <dir> <label>
  local dir="$1" label="$2"
  echo "  $label ($dir):"
  if [ -d "$dir" ] && [ -n "$(find "$dir" -maxdepth 4 -mindepth 1 2>/dev/null)" ]; then
    find "$dir" -maxdepth 4 2>/dev/null | sort | sed "s|^$dir|   .|"
  else
    echo "    (empty or not created yet)"
  fi
}

show_file() {
  # show_file <path> <label> [max_lines]
  local path="$1" label="$2" max_lines="${3:-15}"
  echo "  $label ($path):"
  if [ -f "$path" ]; then
    sed 's/^/    | /' "$path" | tail -n "$max_lines"
  else
    echo "    (not present)"
  fi
}

wait_until_glob() {
  # wait_until_glob <dir> <name-pattern> <timeout-seconds> <description>
  local dir="$1" pattern="$2" timeout="$3" description="$4"
  echo -n "  Waiting up to ${timeout}s for $description "
  local waited=0 found=""
  while [ "$waited" -lt "$timeout" ]; do
    found="$(find "$dir" -maxdepth 4 -name "$pattern" 2>/dev/null | sort | head -1)"
    [ -n "$found" ] && break
    sleep 2
    waited=$((waited + 2))
    echo -n "."
  done
  echo
  if [ -n "$found" ]; then
    echo "  -> appeared after ~${waited}s: $found"
  else
    echo "  -> still nothing matching '$pattern' under $dir after ${timeout}s"
    echo "     (the loop may just need more time -- check: tmux attach -t <session>)"
  fi
}

run() {
  echo "+ $*"
  "$@"
}

ls_pad() { printf '%04d' "$1"; }

# --- 0. sanity ------------------------------------------------------------------------

banner "NGTCalibrationLoop live walkthrough"
note "calibration=$CALIBRATION  runs=$NUM_RUNS  ls-per-run=$LS_PER_RUN  base-run=$BASE_RUN"
note "\$NGT_DEV_HOME=$NGT_DEV_HOME"
if [ ! -f "$REPO_DIR/.venv/bin/activate" ]; then
  echo "No venv at $REPO_DIR/.venv -- run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt" >&2
  exit 1
fi
command -v tmux >/dev/null || { echo "tmux is required" >&2; exit 1; }
pause

# --- 1. setup ---------------------------------------------------------------------------

banner "1. Setting up the scratch environment"
note "Stopping any already-running $CALIBRATION sessions first: a stale process left over"
note "from an earlier run would still be latched onto an old run number that setup's fresh"
note "oms_runs.json won't contain, which crashes it instead of just waiting for a new run."
run "$LIVE_DEMO" stop-all "$CALIBRATION" || true
run "$LIVE_DEMO" setup
show_tree "$NGT_DEV_HOME/calibrationYAML" "Patched calibration YAMLs"
show_file "$NGT_DEV_HOME/ngtParameters.jsn" "ngtParameters.jsn (paths redirected into \$NGT_DEV_HOME)"
show_file "$NGT_DEV_HOME/calibrationYAML/$CALIBRATION.yaml" "$CALIBRATION.yaml (file_in_path/cmssw_base_path repointed)" 8
pause

# --- 2. launch the three loops ------------------------------------------------------------

banner "2. Launching Step 2 + 3 + 4 in tmux (each writes NGTLoopStepN_ALL.log + a tee'd console log)"
run "$LIVE_DEMO" start-all "$CALIBRATION"
sleep 2
run "$LIVE_DEMO" status
note "console output is also tee'd to $NGT_DEV_HOME/logs/tmux_console_Step{2,3,4}_${CALIBRATION}.log"
note "attach any time with: tmux attach -t NGTDemo2_${CALIBRATION}   (Ctrl-b d to detach)"
pause

RUN_DATA_DIR="$NGT_DEV_HOME/data/$CALIBRATION"

for i in $(seq 1 "$NUM_RUNS"); do
  RUN=$((BASE_RUN + i - 1))
  RUN_DIR="$RUN_DATA_DIR/run$RUN"
  FIRST_LS=51
  LAST_SEEDED_LS=$FIRST_LS

  banner "3.$i  Run $RUN ($i/$NUM_RUNS): latching onto a new live run"
  run "$LIVE_DEMO" seed-run "$CALIBRATION" "$RUN" --ls "$FIRST_LS"
  run "$LIVE_DEMO" list-runs
  show_tree "$NGT_DEV_HOME/eos/$CALIBRATION" "Fake EOS area"
  pause

  wait_until_glob "$RUN_DATA_DIR" "run${RUN}" "$STEP2_WAIT_TIMEOUT" "Step 2 to latch run $RUN (creates its working dir)"
  show_tree "$RUN_DIR" "Run $RUN working dir"
  show_file "$RUN_DIR/runStart.log" "runStart.log"
  pause

  banner "3.$i.a  Waiting for Step 2 to process LS $FIRST_LS"
  wait_until_glob "$RUN_DIR" "run${RUN}_LS*.root" "$STEP2_WAIT_TIMEOUT" "Step 2's express job output (*.root)"
  show_tree "$RUN_DIR" "Run $RUN working dir"
  step2_log="$(find "$RUN_DIR" -maxdepth 1 -name 'run*_step2.log' 2>/dev/null | sort | tail -1)"
  [ -n "$step2_log" ] && show_file "$step2_log" "Latest Step 2 job log (shows the fake cmsRun output)"
  pause

  if [ "$LS_PER_RUN" -gt 1 ]; then
    for extra in $(seq 2 "$LS_PER_RUN"); do
      LS=$((FIRST_LS + extra - 1))
      LAST_SEEDED_LS=$LS
      banner "3.$i.b$extra  Simulating a new lumisection arriving: run $RUN, LS $LS"
      run "$LIVE_DEMO" add-ls "$CALIBRATION" "$RUN" "$LS"
      show_tree "$NGT_DEV_HOME/eos/$CALIBRATION" "Fake EOS area"
      pause

      wait_until_glob "$RUN_DIR" "run${RUN}_LS*ls$(ls_pad "$LS")*.root" "$STEP2_WAIT_TIMEOUT" "Step 2 to pick up LS $LS"
      # Step2's output filenames encode an LS *range*, not the raw ls number, so
      # fall back to just re-showing the whole run dir if the exact pattern above
      # doesn't match (it won't -- kept the wait above for its polling delay/log).
      show_tree "$RUN_DIR" "Run $RUN working dir"
      pause
    done
  fi

  banner "3.$i.c  Waiting for Step 3 to batch Step 2's output into an ALCARECO job"
  wait_until_glob "$RUN_DIR" "alcaPromptJob*" "$STEP34_WAIT_TIMEOUT" "an alcaPromptJobNNN directory"
  show_tree "$RUN_DIR" "Run $RUN working dir"
  alca_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'alcaPromptJob*' 2>/dev/null | sort | tail -1)"
  if [ -n "$alca_dir" ]; then
    show_file "$alca_dir/stdout.log" "Latest ALCA job stdout (fake cmsDriver.py + cmsRun output)"
    [ -f "$alca_dir/ALCAOUTPUT.sh" ] && show_file "$alca_dir/ALCAOUTPUT.sh" "ALCAOUTPUT.sh (deletes itself on success -- caught it before cleanup!)" 40
  fi
  pause

  banner "3.$i.d  Waiting for Step 4 to harvest the ALCARECO output"
  wait_until_glob "$RUN_DIR" "harvestJob*" "$STEP34_WAIT_TIMEOUT" "a harvestJobNNN directory"
  show_tree "$RUN_DIR" "Run $RUN working dir"
  harvest_dir="$(find "$RUN_DIR" -maxdepth 1 -name 'harvestJob*' 2>/dev/null | sort | tail -1)"
  if [ -n "$harvest_dir" ]; then
    metadata_file="$(find "$harvest_dir" -maxdepth 1 -name 'NGTCalib*.txt' 2>/dev/null | head -1)"
    [ -n "$metadata_file" ] && show_file "$metadata_file" "condDB upload metadata"
    show_file "$harvest_dir/stdout.log" "Latest harvest job stdout (fake cmsRun + fake uploadConditions.py)"
    [ -f "$harvest_dir/HARVESTING.sh" ] && show_file "$harvest_dir/HARVESTING.sh" "HARVESTING.sh (this one doesn't self-delete)" 40
  fi
  pause

  banner "3.$i.e  Ending run $RUN"
  run "$LIVE_DEMO" end-run "$RUN"
  wait_until_glob "$RUN_DIR" "runEnd.log" "$STEP2_WAIT_TIMEOUT" "Step 2 to finalize the run"
  show_file "$RUN_DIR/runEnd.log" "runEnd.log"
  show_file "$RUN_DIR/allLSProcessed.log" "allLSProcessed.log"
  show_file "$RUN_DIR/expectedOutputs.log" "expectedOutputs.log"
  pause

  banner "3.$i.f  Waiting for Step 3 + Step 4 to finalize run $RUN"
  wait_until_glob "$RUN_DIR" "allStep2FilesProcessed.log" "$STEP34_WAIT_TIMEOUT" "Step 3's final cleanup"
  show_file "$RUN_DIR/allStep2FilesProcessed.log" "allStep2FilesProcessed.log (Step 3's summary)"
  wait_until_glob "$RUN_DIR" "allStep3FilesProcessed.log" "$STEP34_WAIT_TIMEOUT" "Step 4's final cleanup"
  show_file "$RUN_DIR/allStep3FilesProcessed.log" "allStep3FilesProcessed.log (Step 4's summary)"
  pause
done

# --- 4. summary -------------------------------------------------------------------------

banner "4. Summary"
show_tree "$RUN_DATA_DIR" "Everything produced for $CALIBRATION"
run "$LIVE_DEMO" status
echo
echo "Logs:"
echo "  Python logging output: $NGT_DEV_HOME/logs/$CALIBRATION/NGTLoopStep{2,3,4}_ALL.log"
echo "  Raw tmux console:      $NGT_DEV_HOME/logs/tmux_console_Step{2,3,4}_${CALIBRATION}.log"

if [ "$KEEP_RUNNING" = "1" ]; then
  echo
  echo "Leaving tmux sessions running (--keep-running). Stop later with:"
  echo "  $LIVE_DEMO stop-all $CALIBRATION"
  exit 0
fi

if [ "$AUTO_CONTINUE" = "1" ]; then
  echo
  echo "(--yes: leaving tmux sessions running. Stop with: $LIVE_DEMO stop-all $CALIBRATION)"
  exit 0
fi

echo
read -rp "Tear down the tmux sessions now? [y/N] " reply
if [[ "$reply" =~ ^[Yy] ]]; then
  run "$LIVE_DEMO" stop-all "$CALIBRATION"
else
  echo "Left running. Stop later with: $LIVE_DEMO stop-all $CALIBRATION"
fi
