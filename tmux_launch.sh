#!/bin/bash

while getopts "c:" opt; do
  case $opt in
    c) CALIBRATION="$OPTARG" ;;
    *) echo "Usage: $0 -c EcalPedestals|SiStripBad|BeamSpot"; exit 1 ;;
  esac
done

if [[ "$CALIBRATION" != "EcalPedestals" && "$CALIBRATION" != "SiStripBad" && "$CALIBRATION" != "BeamSpot" ]]; then
  echo "Error: calibration -c must be EcalPedestals, SiStripBad, or BeamSpot"
  exit 1
fi

FAILED_STEPS=()

for STEP in 2 3 4; do
  SESSION="CalibrationLoop${STEP}_$CALIBRATION"

  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session $SESSION already exists; skipping"
  elif tmux new-session -d -s "$SESSION" "python3 NGTLoopStep${STEP}.py -c $CALIBRATION; bash"; then
    echo "Launched $SESSION"
  else
    echo "Error: failed to launch $SESSION" >&2
    FAILED_STEPS+=("$STEP")
  fi
done

echo "Finished launching tmux sessions for $CALIBRATION calibration"
if ((${#FAILED_STEPS[@]} > 0)); then
  echo "Error: There were errors launching following steps: ${FAILED_STEPS[*]}"
fi
