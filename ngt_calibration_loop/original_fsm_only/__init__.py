"""Helpers that only the original (pre-Airflow) FSM orchestration used.

The hand-rolled NGTLoopStep3/4.py state machines discovered work by scanning the
working directory: `find_new_run` looked for `run*` folders no processor had latched
yet, and (Step 3) `check_files_for_processing` decided every polling cycle from the
previous step's witness files. No Airflow design calls these -- Step 2's run detector
latches runs (`step2.find_new_run`) and hands each process DAG the run number, and the
per-file/AssetWatcher designs hand it the exact file(s) to work on -- so they live
here, still unit-tested (tests/test_original_fsm_only.py), instead of in the
step3/step4 modules the DAGs import.

`step3.py`/`step4.py` here mirror the modules the functions were split from; they
import everything else (RunContext, CycleDecision, the log-name constants) from
those modules, never the other way round, so this package can be dropped as a unit.
"""
