# The NGT Demonstrator Calibration Leg

## Overview
This directory implements the calibration leg workflow of our NGT demonstrator: monitoring
ongoing collisions at CMS and rederiving calibrations (for now only EcalPedestals and SiStrip
Bad Components) up to the upload to the conditions database of CMS. The pipeline has three
steps, each of which monitors input data, processes it, and produces the output for the next
step:

- **Step 2**: Monitors OMS for new runs, processes raw files files from EOS, produces (RE)RECO files
- **Step 3**: Monitors for step 2 output files, merges them, produces ALCARECO root files
- **Step 4**: Monitors for step 3 output files, performs harvesting, produces and uploads final payload to condDB to be consumed by the NGT Global Tag

The processing logic for each step lives in `ngt_calibration_loop/step{2,3,4}.py`, and is
orchestrated by **Apache Airflow** (`airflow_dags/ngt_dags.py`) rather than the hand-rolled
finite state machines + `while True` polling loops the demonstrator originally used for 2025
data taking. See "Why Airflow" below for the motivation, and `loop_diagram.md` for the original
FSM design this replaced (kept as background -- the batching/latching *logic* it documents is
unchanged, only how it's scheduled and retried).

### Workflow Data Flow

```
EOS RAW Files
    ↓
┌─────────────────────────────────────┐
│ Step 2: Raw → RECO                  │
│ - Monitors OMS for runs             │
│ - Processes LS incrementally        │
│ - Output: run*_LS*_step2.root       │
└─────────────────────────────────────┘
    ↓ (witness files: *_step2_job.txt)
┌─────────────────────────────────────┐
│ Step 3: RECO → ALCARECO             │
│ - Monitors for Step 2 outputs       │
│ - Batches files into ALCA jobs      │
│ - Output: PromptCalibProd*.root     │
└─────────────────────────────────────┘
    ↓ (witness files: step3_job.txt)
┌─────────────────────────────────────┐
│ Step 4: ALCARECO → DB               │
│ - Monitors for Step 3 outputs       │
│ - Harvests calibration constants    │
│ - Uploads to condDB (own retry)     │
└─────────────────────────────────────┘
```

## Why Airflow

The original FSM scripts launched CMSSW jobs (and, in Step 4, the production condDB upload)
via `subprocess.Popen(...)` and never checked the result -- a failed job just vanished, with
no retry and no monitoring beyond grepping log files. Airflow gives every launched job real
retries, timeouts, and UI/task-history visibility, and decouples Step 4's condDB upload from
its harvesting run so a transient upload failure retries on its own without redoing
ALCAHARVEST.

**Design**: per `(calibration, step)` pair there are two DAGs (18 total, for the 3
calibrations x 3 steps):

- `ngt_step{N}_{calibration}_latch` -- cron-scheduled every 30s, looks for a new LHC run to
  latch onto (same logic the old FSM's `NewRunAvailable`/`NewRunAppeared` used), and when
  found triggers the processor DAG for it.
- `ngt_step{N}_{calibration}_process` -- triggered only externally, with
  `{"run_number": ..., "cycle": ...}` in its conf. Each DAG run does exactly one
  check-batch-launch cycle (mirroring the FSM's `WaitingForLS -> CheckingLSForProcess ->
  Preparing* -> Launching* -> Cleanup` loop): `check_and_batch` decides what's ready,
  `prepare_job` writes the cmsDriver script, `launch_job` runs it **synchronously** (with
  retries/timeout -- this is the actual fix, an Airflow task can safely block on a job the way
  the old Popen-based loop couldn't), Step 4 additionally has a separate `upload_conditions`
  task with its own retry policy, then `finalize_cycle`/`advance` either retrigger the same
  processor DAG for the next cycle of the same run, or -- once the run is finished -- trigger
  the next step's processor DAG.

This keeps the FSM's natural per-run audit unit (an operator still thinks "how did run 398600
go") while making every launched job Airflow-native. Lumisection/file arrival is used only as
a *trigger signal* for each cycle, not as the unit of retryable work: Step 2's batching
(`maximumFilesPerJob`) and especially Step 4 (which re-harvests *all* accumulated ALCARECO
files together, every cycle) depend on batching multiple LS/files into one CMSSW job, which
one-task-per-file would fight against.

Two behavior changes from the original FSM were needed because Airflow DAG runs, unlike the
old long-lived process, don't keep in-memory state across cycles:
1. Step 3/4 job directories (`alcaPromptJob_<hash>`, `harvestJob_<hash>`) are named from a
   content hash of their input files instead of a monotonic counter, so a retried task
   reproduces the same directory instead of double-submitting.
2. "Already processed" bookkeeping (`allLSProcessed.log` etc.) is written incrementally after
   every cycle instead of only at final cleanup, and read back at the top of each new cycle.

**Known limitation**: `prepare_job`/`launch_job`/`finalize_cycle` each independently rebuild
the job spec (cheap and idempotent, given point 1 above) rather than passing it through XCom,
which is simple but means the job's temp script/metadata gets rewritten a few times per cycle
-- harmless, but a candidate for a future cleanup pass if it matters for a given deployment.

**Scope note**: this is built and fully tested against the offline fake OMS/EOS/CMSSW
toolchain (`dev/bin/`, `tests/stubs/`, `dev/seed.py`) -- the same one the project's pytest
suite and live demo already used before this migration. Deploying it onto the real CERN P5
calibration nodes (real OMS, real EOS/kerberos, real CMSSW, real condDB) is a separate,
not-yet-done step; see "Deploying on the calibration nodes" below for what that would involve.

## Setting up and running Airflow

Airflow has no official Windows support -- run it from a Linux host (WSL works fine on
Windows). This repo
targets Airflow 2.10.x on Python 3.12, with `LocalExecutor` against a local Postgres instance
(needed for real task parallelism across the 18 DAGs; SQLite only supports `SequentialExecutor`,
which would serialize every task across every calibration/step).

```bash
# 1. Airflow venv (separate from the repo's .venv used for plain pytest)
python3 -m venv ~/airflow-ngt-venv
source ~/airflow-ngt-venv/bin/activate
pip install "apache-airflow==2.10.5" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.5/constraints-3.12.txt"
pip install psycopg2-binary
pip install -e .                       # ngt_calibration_loop, used by the DAGs
pip install -e tests/stubs             # the omsapi test-stub, see its note below

# 2. Postgres (one-time, needs sudo)
sudo apt-get install -y postgresql
sudo -u postgres psql -c "CREATE ROLE airflow_ngt WITH LOGIN PASSWORD 'airflow_ngt';"
sudo -u postgres psql -c "CREATE DATABASE airflow_ngt OWNER airflow_ngt;"

# 3. Initialize Airflow against that DB
source dev/airflow_env.sh              # AIRFLOW_HOME, executor, DB, DAGs folder
airflow db migrate
airflow users create --username admin --password admin \
  --firstname NGT --lastname Admin --role Admin --email admin@example.invalid
```

Why `pip install -e tests/stubs`: the fake `omsapi` package (see below) needs to be importable
by Airflow's scheduler subprocesses. `PYTHONPATH` set in the launching shell was not reliably
observed to reach those forked/spawned subprocesses in testing, so the stub is installed as a
real (tiny) package instead -- see `dev/airflow_demo.sh`'s `demo_env_exports` for the note.

`dev/airflow_demo.sh setup`/`start`/`stop`/`unpause` wrap steps 1-3 above and the day-to-day
webserver+scheduler lifecycle -- see "Running a live demo" below for the full walkthrough.

### Deploying on the calibration nodes

Not done as part of this migration -- would need, on `ngtcalfu-c2b05-{43,44}-01.cms` (or
wherever Airflow itself runs, which does not need to be the same host CMSSW/EOS access is
needed on, since `launch_job`/`upload_conditions` just run local shell scripts): the real
`oms-api-client` (`git clone ssh://git@gitlab.cern.ch:7999/cmsoms/oms-api-client.git`, `cp -r
oms-api-client/omsapi` next to `ngt_calibration_loop/`, same as the old `NGTLoopStep2.py` setup
required) instead of `tests/stubs/omsapi`, the kerberos cron job from the original setup
instructions for EOS access, `COND_AUTH_PATH` credentials for the condDB upload, and a
production-grade Airflow deployment (Postgres/MySQL metadata DB, `LocalExecutor` or
`CeleryExecutor`, the DAGs folder pointed at this repo's `airflow_dags/`).

## Package layout

- `ngt_calibration_loop/` -- the processing logic (OMS/EOS querying, cmsDriver script
  preparation, batching decisions), as plain stateless functions with no Airflow dependency.
  `config.py`/`oms.py`/`eos.py`/`shell.py` are shared helpers; `step2.py`/`step3.py`/`step4.py`
  hold each step's logic. `shell.run_job_script` is the one seam that actually launches a job
  script, blocking until it completes -- this is what replaced the old `subprocess.Popen(...)`
  fire-and-forget calls.
- `airflow_dags/ngt_dags.py` -- the DAG factory described above.
- `calibrationYAML/` -- per-calibration config (unchanged format from the original FSM setup).
- `tests/` -- see "Running the tests" below.
- `dev/` -- the offline fake toolchain and demo scripts, see "Running a live demo" below.

### Directory structure produced by a run

```
$DATA_BASE_PATH/{calibration}/
└── run{run_number}/
    ├── runStart.log            # Step 2: ISO timestamp
    ├── runEnd.log              # Step 2: signals completion
    │
    ├── cmsDriver_<hash>.sh     # Step 2: job script (self-deletes on success)
    ├── run*_LS*_step2.py       # Step 2: CMSSW Python configs
    ├── run*_LS*_step2.log      # Step 2: job logs
    ├── run*_LS*_step2.root     # Step 2: RECO output files
    ├── run*_LS*_step2_job.txt  # Step 2: witness files
    │
    ├── allLSProcessed.log              # Step 2: incrementally-updated processed-LS log
    ├── expectedOutputs.log             # Step 2: expected Step 2 outputs
    ├── allStep2FilesProcessed.log      # Step 3: incrementally-updated processed-files log
    ├── allStep3FilesProcessed.log      # Step 4: rewritten each cycle with the full harvested set
    │
    ├── alcaPromptJob_<hash>/           # Step 3: one ALCA job per distinct input-file batch
    │   ├── ALCAOUTPUT.sh
    │   ├── run*_step3.py
    │   ├── stdout.log / stderr.log
    │   ├── PromptCalibProd*.root
    │   └── *_job.txt                   # witness file
    │
    └── harvestJob_<hash>/              # Step 4: one harvest job per distinct input-file set
        ├── HARVESTING.sh               # cmsRun ALCAHARVEST only
        ├── UPLOAD.sh                   # uploadConditions.py -- separate, independently retried
        ├── run*_step4.py
        ├── stdout.log / stderr.log
        ├── upload_stdout.log / upload_stderr.log
        ├── {metadata_filename}.txt
        └── {final_db_name}.db
```

(`alcaPromptJob_<hash>`/`harvestJob_<hash>` replace the old FSM's sequentially-numbered
`alcaPromptJob000`/`harvestJob000` -- see "Why Airflow" above for why.)

## Running the tests

The `ngt_calibration_loop` logic and the DAG wiring are both covered by pytest, with every
external dependency mocked -- no CMSSW, EOS, OMS, condDB, or CERN-internal hosts needed.

```bash
python3 -m venv .venv          # or use the same venv as above
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

- `tests/test_step{2,3,4}_lib.py` -- exercise `ngt_calibration_loop`'s functions directly:
  run latching, batching thresholds, timeout/run-ended escape hatches, job prep/launch,
  and (new) that a failed launch raises so the calling Airflow task would retry.
  - OMS REST API -> `tests/stubs/omsapi` (in-memory fake of the query-builder interface;
    the real `oms-api-client` isn't installed for tests, only for real deploys)
  - `edmFileUtil`/`xrdfs` (EOS access) -> `tests/support/fake_subprocess.FakeEOS`
  - job launches -> `ngt_calibration_loop.shell.run_job_script` is replaced by the
    `job_runner` fixture, a recorder that can also be told to raise on demand
    (`job_runner.queue_failure()`) to exercise the retry-triggering path
  - filesystem paths -> redirected into a pytest `tmp_path` via `NGT_PARAMETERS_PATH`/
    `NGT_CALIBRATION_YAML_DIR`
- `tests/test_dags.py` -- DagBag import-error/structure checks plus direct unit tests of each
  task's `python_callable` (no live Airflow scheduler/DB needed -- `trigger_dag` calls are
  monkeypatched out). Guarded by `pytest.importorskip("airflow")`, so it's automatically
  skipped wherever `apache-airflow` isn't installed (e.g. a plain Windows `.venv`) and the rest
  of the suite still runs. Where Airflow *is* installed, run `source dev/airflow_env.sh` first
  so `import airflow` picks up the right `AIRFLOW_HOME`/DB config.

## Running a live demo (no CERN infra needed)

`dev/airflow_demo.sh` runs the real Airflow webserver + scheduler (in tmux) against the same
fake OMS/EOS/CMSSW toolchain in `dev/bin/` (fake `cmsDriver.py`, `cmsRun`, `edmFileUtil`,
`xrdfs`, `cmsrel`, `cmsenv`, `uploadConditions.py`) and `dev/seed.py` used by the tests.
Everything runs for real -- real DAG runs, real generated job scripts, real output files
flowing Step 2 -> Step 3 -> Step 4 to a fake condDB upload -- entirely offline. Airflow has
no official Windows support, so this needs a Linux host (WSL works fine on Windows).

```bash
./dev/airflow_demo.sh setup                                # create ./demo-env scratch env
./dev/airflow_demo.sh start                                 # webserver (:8090) + scheduler, tmux
./dev/airflow_demo.sh unpause EcalPedestals                 # unpause its 6 DAGs
./dev/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52  # latch a fake live run
./dev/airflow_demo.sh ui                                    # print the UI URL + login
./dev/airflow_demo.sh add-ls EcalPedestals 398600 53         # simulate a new lumisection arriving
./dev/airflow_demo.sh end-run 398600                         # simulate the run ending
./dev/airflow_demo.sh status                                 # list running tmux sessions
./dev/airflow_demo.sh stop                                   # tear down webserver + scheduler
```

For a guided, narrated walkthrough that pauses between steps and shows you the relevant output
directories/files and DAG-run history so you can watch the latch -> process(xN cycles) ->
handoff chain actually happening (plus a demonstration of a forced job failure retrying), use:

```bash
./dev/airflow_interactive_demo.sh                                    # EcalPedestals, run 398600, 2 LS
./dev/airflow_interactive_demo.sh --calibration SiStripBad --ls 4
./dev/airflow_interactive_demo.sh --yes                              # don't pause between steps
```

Run `./dev/airflow_demo.sh` with no arguments for the full command list. All demo *data* lives
under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory inside this repo) --
delete it any time to reset the demo environment (the Airflow instance itself -- metadata DB,
`AIRFLOW_HOME` -- is separate/persistent, see "Setting up and running Airflow" above).

Two settings are overridable via environment variables:

| Variable                 | Default                              | Meaning                          |
|---------------------------|--------------------------------------|-----------------------------------|
| `NGT_DEV_HOME`             | `./demo-env`                         | scratch state directory           |
| `NGT_LOOP_SLEEP_SECONDS`   | `10` (production default: `60`)      | Step 3/4 cycle-retrigger delay, seconds |

e.g. `NGT_DEV_HOME=/tmp/ngt-demo ./dev/airflow_demo.sh setup`. `NGT_DEV_HOME` is read at
`setup` time and baked into `$NGT_DEV_HOME/ngtParameters.jsn`, so re-run `setup` after
changing it. `NGT_LOOP_SLEEP_SECONDS` is read fresh by the `advance` task each time it
retriggers the next cycle.

## Nota Bene
There are quite a lot of issues remaining, still. The FSM-based version this replaced was the
"functioning" version used for the demonstrator in the 2025 data taking; this Airflow-based
version is the planned improvement for 2026 data-taking, built and tested offline as described
above. Actual rollout onto the P5 calibration nodes is separate future work -- see "Deploying
on the calibration nodes".
