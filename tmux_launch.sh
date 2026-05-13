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

tmux new-session -d -s CalibrationLoop2_$CALIBRATION "python3 NGTLoopStep2.py -c $CALIBRATION; bash"
tmux new-session -d -s CalibrationLoop3_$CALIBRATION "python3 NGTLoopStep3.py -c $CALIBRATION; bash"
tmux new-session -d -s CalibrationLoop4_$CALIBRATION "python3 NGTLoopStep4.py -c $CALIBRATION; bash"

echo "Launched CalibrationLoop2/3/4 with -c $CALIBRATION"
