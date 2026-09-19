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
orchestrated by **Apache Airflow** (`airflow_automation/airflow_dags/`) rather than the hand-rolled
finite state machines + `while True` polling loops the demonstrator originally used for 2025
data taking. See "Why Airflow" below for the motivation. The batching/latching *logic* is
unchanged from the FSMs, only how it's scheduled and retried.

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

**Design**: every CMSSW job is launched **synchronously** from an Airflow task, with retries and
a timeout (this is the actual fix -- an Airflow task can safely block on a job the way the old
Popen-based loop couldn't), and Step 4's condDB upload is a separate `step4_upload` task with
its own retry policy. Two DAG structures are built on this -- a per-file design and an
event-driven `AssetWatcher` design, see "Airflow DAG designs" below -- and both run one input
file through Step 2 -> Step 3 -> Step 4 as a straight
`step2_express >> step3_alca >> step4_harvest >> step4_upload` chain. Lumisection/file arrival is
only a *trigger signal* for processing; Step 4 re-harvests *all* accumulated ALCARECO files
together every time, so more statistics improve the payload.

Two behavior changes from the original FSM were needed because Airflow DAG runs, unlike the
old long-lived process, don't keep in-memory state across cycles:
1. Step 3/4 job directories (`alcaPromptJob_<hash>`, `harvestJob_<hash>`) are named from a
   content hash of their input files instead of a monotonic counter, so a retried task
   reproduces the same directory instead of double-submitting.
2. "Already processed" bookkeeping (`allLSProcessed.log` etc.) is written incrementally after
   every cycle instead of only at final cleanup, and read back at the top of each new cycle.

**Airflow 3 note**: cross-DAG coordination -- triggering another DAG's run -- goes through
`airflow_automation/airflow_dags/airflow_api.py`, a small wrapper around Airflow's stable REST API, rather than
`airflow.api.common.trigger_dag`/direct `DagRun`/`TaskInstance` ORM queries (what this repo
used under Airflow 2, and what Airflow 3 no longer permits from task code at all -- see its
"Upgrading to Airflow 3" docs on removed direct database access). Auth uses the Simple Auth
Manager's zero-credential admin-token endpoint, appropriate for this offline single-user dev
instance specifically (see `airflow_automation/airflow_demo/airflow_env.sh`).

**Scope note**: this is built and fully tested against the offline fake OMS/EOS/CMSSW
toolchain (`scenario-player/bin/`, `tests/stubs/`, `scenario-player/seed.py`) -- the same one the project's pytest
suite and live demo already used before this migration. Deploying it onto the real CERN P5
calibration nodes (real OMS, real EOS/kerberos, real CMSSW, real condDB) is a separate,
not-yet-done step; see "Deploying on the calibration nodes" below for what that would involve.

## Setting up and running Airflow

Airflow has no official Windows support -- run it from a Linux host (WSL works fine on
Windows). This repo targets **Airflow 3.3.1** on Python 3.12, with `LocalExecutor` against a
local Postgres instance (needed for real task parallelism across the DAGs; SQLite only
supports `SequentialExecutor`, which would serialize every task across every
calibration).

```bash
# 1. Airflow venv (separate from the repo's .venv used for plain pytest)
python3 -m venv ~/airflow3-ngt-venv
source ~/airflow3-ngt-venv/bin/activate
pip install "apache-airflow==3.3.1" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.12.txt"
pip install psycopg2-binary asyncpg    # asyncpg: Airflow 3 also opens an async
                                        # engine against the metadata DB
pip install -e .                       # ngt_calibration_loop, used by the DAGs
pip install -e tests/stubs             # the omsapi test-stub, see its note below

# 2. Postgres (one-time, needs sudo). Named distinctly from any pre-existing
#    Airflow 2.x install's database rather than reusing it in place -- see
#    airflow_automation/airflow_demo/airflow_env.sh's comment for why.
sudo apt-get install -y postgresql
sudo -u postgres psql -c "CREATE ROLE airflow_ngt WITH LOGIN PASSWORD 'airflow_ngt';"  # skip if it already exists
sudo -u postgres psql -c "CREATE DATABASE airflow3_ngt OWNER airflow_ngt;"

# 3. Initialize Airflow against that DB
source airflow_automation/airflow_demo/airflow_env.sh              # AIRFLOW_HOME, executor, DB, DAGs folder, auth
airflow db migrate
```

No `airflow users create` step: `airflow_automation/airflow_demo/airflow_env.sh` sets
`AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True`, so Airflow 3's Simple Auth Manager (the
new default, replacing FAB) treats every request as admin with no login at all -- appropriate
for this offline, single-user dev/demo instance specifically (that auth manager is explicitly
documented as development/testing-only, which is exactly what this is), not something to carry
into a real deployment.

Why `pip install -e tests/stubs`: the fake `omsapi` package (see below) needs to be importable
by Airflow's scheduler subprocesses. `PYTHONPATH` set in the launching shell was not reliably
observed to reach those forked/spawned subprocesses in testing, so the stub is installed as a
real (tiny) package instead -- see `scenario-player/sim_env.sh`'s `sim_setup` for the note.

`airflow_automation/airflow_demo/airflow_demo.sh setup`/`start`/`stop`/`unpause` wrap steps 1-3 above and the day-to-day
process lifecycle -- see "Running a live demo" below for the full walkthrough. Airflow 3 splits
DAG parsing out of the scheduler into its own mandatory `airflow dag-processor` process (the
scheduler no longer spawns it itself, unlike Airflow 2), so `start`/`stop`/`status` manage four
processes (scheduler, dag-processor, API server, triggerer) instead of the original two. The
triggerer hosts the per-file design's deferred task and the AssetWatcher design's watchers -- see
"Airflow DAG designs" below -- but is started unconditionally since it's harmless when unused.

### Deploying on the calibration nodes

Not done as part of this migration -- would need, on `ngtcalfu-c2b05-{43,44}-01.cms` (or
wherever Airflow itself runs, which does not need to be the same host CMSSW/EOS access is
needed on, since `launch_job`/`upload_conditions` just run local shell scripts): the real
`oms-api-client` (`git clone ssh://git@gitlab.cern.ch:7999/cmsoms/oms-api-client.git`, `cp -r
oms-api-client/omsapi` next to `ngt_calibration_loop/`, same as the old `NGTLoopStep2.py` setup
required) instead of `tests/stubs/omsapi`, the kerberos cron job from the original setup
instructions for EOS access, `COND_AUTH_PATH` credentials for the condDB upload, and a
production-grade Airflow deployment (Postgres/MySQL metadata DB, `LocalExecutor` or
`CeleryExecutor`, a real auth manager instead of Simple Auth Manager's all-admins mode, the
DAGs folder pointed at this repo's `airflow_automation/airflow_dags/`, and the scheduler/dag-processor/API-server
processes run as proper services rather than tmux sessions).

If the calibration nodes are actually a **pool** of worker machines rather than one host, one
more thing needs deciding before rollout: Step 2's output is too large for shared storage to
hold, so Step 2 and Step 3 have to execute on the same machine (Step 2's output staying on that
machine's local disk), while Step 4 -- needing only Step 3's small output -- can run anywhere.
The options this requires (both for a plain Airflow-orchestrated worker pool and for Airflow
submitting to a SLURM/HTCondor-style batch system instead) are analysed in a design write-up
that isn't tracked in this repo yet. Two background references aren't tracked here either: one on
resource management and batch systems in general (an introduction to SLURM and HTCondor for
anyone who hasn't used one, and how Airflow's own resource-management model compares to
Luigi/REANA/Dagster/Prefect/Kestra), and one summarising the real NGT demonstrator's own target
hardware, event rates/sizes, buffer-sizing calculations and processing-time benchmarks (external
CERN/CMS project reports, not this repo's mocked dev environment).

## Package layout

- `ngt_calibration_loop/` -- the processing logic (OMS/EOS querying, cmsDriver script
  preparation, batching decisions), as plain stateless functions with no Airflow dependency.
  `config.py`/`oms.py`/`eos.py`/`shell.py` are shared helpers; `step2.py`/`step3.py`/`step4.py`
  hold each step's logic. `shell.run_job_script` is the one seam that actually launches a job
  script, blocking until it completes -- this is what replaced the old `subprocess.Popen(...)`
  fire-and-forget calls. `original_fsm_only/` holds the few functions only the original
  FSM orchestration used (the Step 3/4 run-directory scan, Step 3's witness-file batching
  decision) -- no Airflow design calls them, they're kept, tested, but out of the step modules
  the DAGs import.
- `airflow_automation/airflow_dags/ngt_dags_per_file.py` / `airflow_automation/airflow_dags/triggers.py` /
  `airflow_automation/airflow_dags/_perfile_process.py` -- the per-file DAG design (run-detector /
  deferred file-detector / per-file processing); `_perfile_process.py` holds the step2/3/4
  processing callables it shares with the AssetWatcher design -- see "Airflow DAG designs" below and
  [`airflow_automation/airflow-designs-docs/per_file_scheduling_design.md`](airflow_automation/airflow-designs-docs/per_file_scheduling_design.md).
- `airflow_automation/airflow_dags/ngt_dags_watch.py` -- an AssetWatcher-driven design: **all assets static, driven by
  `AssetWatcher`s, zero cron DAGs**. One `Asset("ngt://runs")` watched for new runs, three static
  `Asset("ngt://files/<cal>")` watched for that run's new files (one event per file); watermarks
  in each Asset's `AssetStateStore`.
  See [`airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md`](airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md).
- `airflow_automation/airflow_dags/airflow_api.py` -- small wrapper around Airflow's stable REST API, used by
  the per-file design for cross-DAG coordination (see "Airflow 3 note" above); the AssetWatcher design needs none.
- `calibrationYAML/` -- per-calibration config (unchanged format from the original FSM setup).
- `tests/` -- see "Running the tests" below.
- `airflow_automation/` -- everything specific to the Airflow prototypes: `airflow_dags/` (the DAG
  designs above), `airflow-designs-docs/` (their write-ups) and `airflow_demo/` (the Airflow
  adapter for the live-test setup below: `airflow_env.sh`, `airflow_demo.sh`, and the
  `*_interactive_demo.sh` walkthroughs). `airflow_dags/` is still imported as the top-level
  package `airflow_dags` -- `airflow_env.sh` and `tests/conftest.py` put `airflow_automation/`
  on the path for that.
- `scenario-player/` -- the engine-agnostic offline simulator, see "Running a live demo" below:
  `sim_env.sh` (scratch environment + fake toolchain bootstrap), `seed.py` and
  `scenario_player.py` (driving/replaying fake runs, with `scenarios/*.yaml`), `faults.py` (the
  shared fault-injection vocabulary) and `bin/` (the fake `cmsRun`/`cmsDriver.py`/... toolchain).
  Nothing in it imports or knows about Airflow specifically, so the same scratch environment and
  scenario files can drive a future non-Airflow adapter unchanged -- see `sim_env.sh`'s header
  comment for why that split exists. It's not a Python package (hence the hyphen): `faults.py`,
  `seed.py` and `scenario_player.py` are top-level modules found via `sys.path`
  (`tests/conftest.py`, and `PYTHONPATH` from `sim_env.sh` for the fake `omsapi`'s live fault path).

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
- `tests/test_original_fsm_only.py` -- the same style of tests for `ngt_calibration_loop/original_fsm_only/`
  (run-directory scan, Step 3's witness-file batching decision).
- `tests/test_scenario_player.py` -- `scenario-player/scenario_player.py`'s timeline-building (ordering,
  concurrent-run merging) and playback (event firing order/arguments), with `scenario-player/seed.py`'s
  functions monkeypatched out -- no filesystem/`$NGT_DEV_HOME` access needed.
- `tests/airflow_designs/test_dags_per_file.py` -- DagBag import-error/structure checks plus direct unit
  tests of each task's `python_callable` for the per-file design (no live Airflow
  scheduler/DB/API server needed -- `airflow_dags.airflow_api`'s functions are monkeypatched out),
  and `triggers.NewFileTrigger`'s async `run()` driven to its first yielded event with a single
  `asyncio.run()` call (no `pytest-asyncio` needed). Guarded by `pytest.importorskip("airflow")`, so
  it's automatically skipped wherever `apache-airflow` isn't installed (e.g. a plain Windows `.venv`)
  and the rest of the suite still runs. Where Airflow *is* installed, run
  `source airflow_automation/airflow_demo/airflow_env.sh` first so `import airflow` picks up the
  right `AIRFLOW_HOME`/DB config.
- `tests/airflow_designs/test_dags_watch.py` -- same shape, for the AssetWatcher design: DAG wiring
  plus the two watcher triggers driven with a fake `AssetStateStore`. Same `importorskip` guard.

## Running a live demo (no CERN infra needed)

`airflow_automation/airflow_demo/airflow_demo.sh` runs the real Airflow scheduler + dag-processor + API server (in tmux)
against the same fake OMS/EOS/CMSSW toolchain in `scenario-player/bin/` (fake `cmsDriver.py`, `cmsRun`,
`edmFileUtil`, `xrdfs`, `cmsrel`, `cmsenv`, `uploadConditions.py`) and `scenario-player/seed.py` used by the
tests -- bootstrapped via `scenario-player/sim_env.sh`'s `sim_setup`, shared with any future non-Airflow
engine adapter. Everything runs for real -- real DAG runs, real generated job scripts, real
output files flowing Step 2 -> Step 3 -> Step 4 to a fake condDB upload -- entirely offline.
Airflow has no official Windows support, so this needs a Linux host (WSL works fine on
Windows).

```bash
./airflow_automation/airflow_demo/airflow_demo.sh setup                                # create ./demo-env scratch env
./airflow_automation/airflow_demo/airflow_demo.sh start                                 # scheduler + dag-processor + API server (:8090), tmux
./airflow_automation/airflow_demo/airflow_demo.sh unpause-perfile                         # unpause the per-file design's 5 DAGs
./airflow_automation/airflow_demo/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52  # latch a fake live run
./airflow_automation/airflow_demo/airflow_demo.sh ui                                    # print the UI URL
./airflow_automation/airflow_demo/airflow_demo.sh add-ls EcalPedestals 398600 53         # simulate a new lumisection arriving
./airflow_automation/airflow_demo/airflow_demo.sh end-run 398600                         # simulate the run ending
./airflow_automation/airflow_demo/airflow_demo.sh status                                 # list running tmux sessions
./airflow_automation/airflow_demo/airflow_demo.sh stop                                   # tear down all three processes
```

For a guided, narrated walkthrough that pauses between steps and shows you the relevant output
directories/files and DAG-run history so you can watch the run_detector -> file_detector
(deferred) -> per-file process chain actually happening (plus a demonstration of a forced job
failure retrying), use:

```bash
./airflow_automation/airflow_demo/perfile_interactive_demo.sh                                    # EcalPedestals, 2 LS
./airflow_automation/airflow_demo/perfile_interactive_demo.sh --calibration SiStripBad --ls 4
./airflow_automation/airflow_demo/perfile_interactive_demo.sh --yes                              # don't pause between steps
```

### Scripting a timed sequence of runs/lumisections

`seed-run`/`add-ls`/`end-run` above are one-shot commands, handy for typing out by hand or
for the paced walkthrough above. For an unattended, repeatable test -- or to exercise several
calibrations processing concurrently, which is awkward to type out live -- `scenario-player/scenario_player.py`
plays back a whole timeline from a declarative YAML file instead:

```bash
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml            # real-time
python3 scenario-player/scenario_player.py scenario-player/scenarios/concurrent_multi_calibration.yaml --speed 5
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml --dry-run  # print the
                                                                                   # resolved
                                                                                   # timeline only
```

A scenario lists one or more runs, each with a start offset and a sequence of lumisections
separated by delays (`after: N` seconds since the previous event in that run); multiple runs'
events are merged into one global timeline by absolute time. A run is one CMS-wide DAQ run --
there's only ever one active at a time across the whole experiment, so `build_timeline` rejects
a scenario whose runs overlap in time -- but its lumisections can feed several calibrations at
once, since one run's RAW data underlies every calibration stream: each run entry takes a
`calibrations` list (always a list, even for one calibration), and the timeline plays the same
start/LS-arrival/end sequence once per listed calibration while sharing that one run number (see
`scenario-player/scenarios/concurrent_multi_calibration.yaml`). See `scenario-player/scenarios/*.yaml` for more worked
examples and `scenario-player/scenario_player.py`'s module docstring for the full format description.

This is the same engine-agnostic layer `seed-run`/`add-ls`/`end-run` are (it calls
`scenario-player/seed.py`'s functions directly, not Airflow) -- run it against `$NGT_DEV_HOME` while
`./airflow_automation/airflow_demo/airflow_demo.sh start` is running to exercise the current Airflow-based pipeline, or
point a future non-Airflow adapter at the same scratch environment and the same scenario files
should still apply unchanged.

Run `./airflow_automation/airflow_demo/airflow_demo.sh` with no arguments for the full command list. All demo *data* lives
under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory inside this repo) --
delete it any time to reset the demo environment (the Airflow instance itself -- metadata DB,
`AIRFLOW_HOME` -- is separate/persistent, see "Setting up and running Airflow" above).

### Resetting state

Two independent layers of state can go stale, and clearing one doesn't clear the other:

- **The simulator's own state** (`$NGT_DEV_HOME`: fake OMS runs, EOS files, output data, logs)
  -- via `scenario-player/sim_env.sh`, run directly, no Airflow-specific setup needed:
  ```bash
  ./scenario-player/sim_env.sh reset   # wipes $NGT_DEV_HOME entirely
  ./scenario-player/sim_env.sh setup   # recreates it (needs an active venv -- see note below)
  ```
  `setup`'s `pip install -e tests/stubs` step needs **some** Python venv active first (either
  `.venv` or `~/airflow3-ngt-venv`, whichever you're using) -- without one, Debian/Ubuntu's
  system Python refuses the install outright (PEP 668,
  `error: externally-managed-environment`). Forgetting this now fails fast with a clear message
  telling you to `source .../activate` first, rather than that pip error. `airflow_automation/airflow_demo/airflow_demo.sh
  setup` (which calls the same `sim_setup` internally) always activates `~/airflow3-ngt-venv`
  itself first, so this only comes up when running `scenario-player/sim_env.sh` directly.

  **Caution**: `./scenario-player/sim_env.sh reset` deletes `$NGT_DEV_HOME` unconditionally, with no
  awareness of whether an engine's processes are currently running against it -- confirmed live,
  doing this while Airflow's triggerer had an in-flight deferred task (`ngt_dags_per_file.py`'s
  `wait_for_files`) left that task stuck in a running/deferred state indefinitely (needing a
  process restart to recover), and silently orphaned each tmux process's own console log. If
  Airflow is running, use `./airflow_automation/airflow_demo/airflow_demo.sh reset-scenario` instead (below) rather than
  calling this directly.

- **Airflow's own state** (DAG-run/task-instance history, in the metadata DB, *not* under
  `$NGT_DEV_HOME`) -- resetting only the simulator leaves stale DagRuns in Airflow's UI
  referencing run numbers no longer on disk, so `airflow_automation/airflow_demo/airflow_demo.sh` has its own commands:
  ```bash
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-perfile    # clear ngt_dags_per_file.py's 5 DAGs' history only
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-watch      # clear ngt_dags_watch.py's 4 DAGs + its static Assets
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-airflow    # full reset: both designs, variables, connections --
                                          # everything. Stops/restarts the 4 tmux processes
                                          # around it automatically if they were running (each
                                          # one crashes on its own next DB query otherwise --
                                          # they hold in-memory state tied to the old schema)
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-scenario   # wipes + recreates $NGT_DEV_HOME (scenario-player/sim_env.sh's own
                                          # reset+setup), stopping/restarting Airflow around it if
                                          # running -- the safe way to reset simulator state
                                          # without hitting the caution above. Doesn't touch
                                          # DAG-run history; pair with the others for that.
  ```
  `reset-perfile`/`reset-watch`/`reset-airflow` don't touch `$NGT_DEV_HOME`; `reset-scenario`
  doesn't touch DAG-run history -- reset the ones you need for a fully clean slate between test
  runs.

Several settings are overridable via environment variables:

| Variable                          | Default                          | Meaning                          |
|-------------------------------------|-----------------------------------|-----------------------------------|
| `NGT_DEV_HOME`                       | `./demo-env`                     | scratch state directory           |
| `NGT_LOOP_SLEEP_SECONDS`             | `10` (production default: `60`)  | Step 3/4 cycle-retrigger delay, seconds |
| `NGT_TEST_MAX_LATCH_TIME_HOURS`      | `0.25` (production default: `8`) | how long a calibration keeps waiting on a run before giving up, hours -- see below |
| `NGT_TEST_STEP_TIMEOUT_SECONDS`      | `900` (production default: `28800`/`32400`) | same, for Step 3/Step 4's own give-up timers, seconds |
| `NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS` | `20` (production default: `1800`) | `ngt_dags_per_file.py`-only: how long `wait_for_files` waits after a run *ends* before giving up -- see below |

e.g. `NGT_DEV_HOME=/tmp/ngt-demo ./airflow_automation/airflow_demo/airflow_demo.sh setup`. `NGT_DEV_HOME` is read at
`setup` time and baked into `$NGT_DEV_HOME/ngtParameters.jsn`, so re-run `setup` after
changing it. `NGT_LOOP_SLEEP_SECONDS` is read fresh by the `advance` task each time it
retriggers the next cycle. `NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS` is read once per DAG
parse (it's a module-level default in `ngt_dags_per_file.py`, not a calibrationYAML key), so
the dag-processor needs to re-parse after changing it.

**Why the last two exist**: `ngt_calibration_loop`'s step2/3/4 each wait up to several
*production* hours (8h/9h/8h) before giving up on a run/calibration that never reconciles --
correct for real OMS/EOS, but not something any live-test scenario should ever actually have to
wait out. `$NGT_DEV_HOME`'s calibrationYAML copies are patched with short values by default (see
`scenario-player/seed.py`'s `FAST_MAX_LATCH_TIME_HOURS`/`FAST_STEP_TIMEOUT_SECONDS`) via an optional,
backward-compatible calibrationYAML key (`step_2_config.maxLatchTimeHours`,
`step_3_config`/`step_4_config.timeoutSeconds` -- absent in production calibrationYAML, so
production behavior is unaffected). This is what actually fixes "a calibration that never gets
matching files for a scenario's run sits waiting/deferred for hours" -- confirmed live -- rather
than leaving it as a solution-internal constant the live-test setup has no way to influence. The
defaults (15 min) are deliberately well above `ngt_perfile_run_detector`'s 30s poll interval and
any realistic scenario/interactive-demo duration -- shortening them further risks a still-live,
still-progressing run's own workflow getting force-finalized mid-scenario (`not still_have_time`
is checked *before* "is there still something to do" in all three steps), not just the
already-caught-up/never-matched calibrations these are meant to unstick. Override either one to
test the give-up-and-finalize path itself, or to give a deliberately long-running scenario more
headroom.

**Why the third (`NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS`) is different**: the two above are
measured from a run's *start*; a run that runs only part of that budget before *ending* would
otherwise still leave `ngt_dags_per_file.py`'s `wait_for_files` task deferred for whatever's left
of it -- confirmed live. Rather than changing `ngt_calibration_loop`'s shared decision logic
(`step2.py`, whose decision logic stays unmodified), this one lives
entirely in `airflow_automation/airflow_dags/triggers.py`'s `NewFileTrigger`: it queries OMS directly for the run's
actual end time and gives up once that's `NGT_FILE_DETECTOR_RUN_END_GRACE_SECONDS` in the past,
independent of `check_ls_for_processing`'s own (unmodified) run-start-relative timeout. See
`per_file_scheduling_design.md` for the full writeup.

## Airflow DAG designs

This repo currently builds **two** independent DAG sets from the same, completely unmodified
`ngt_calibration_loop` processing logic and the same fake OMS/EOS/CMSSW live-test setup --
neither replaces the other; each is unpaused/paused independently so they can be compared
head to head against identical scripted scenarios (`scenario-player/scenario_player.py`,
`scenario-player/scenarios/*.yaml`). Only one design's run-discovery should be unpaused at a time -- they
both share the on-disk `DATA/<cal>/run<N>/` working-dir dedup. Full writeups:
[`per_file_scheduling_design.md`](airflow_automation/airflow-designs-docs/per_file_scheduling_design.md) (the per-file design -- DAG-by-DAG,
sequence diagrams, batching-semantics, polling-vs-defer) and
[`watch_asset_scheduling_design.md`](airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md) (the AssetWatcher design).

**`airflow_automation/airflow_dags/ngt_dags_per_file.py`**: 5 DAGs total for 3 calibrations:

- `ngt_perfile_run_detector` -- one DAG (not one per calibration), cron-scheduled (every 30s),
  with one task per calibration. Dispatches file detection for a
  calibration the moment it latches a new run (reusing `step2.find_new_run` unchanged).
- `ngt_perfile_file_detector` -- one generic DAG (parameterized by `calibration` via trigger
  conf, not one per calibration), triggered per `(calibration, run)`. Its one task **defers**
  (see `airflow_automation/airflow_dags/triggers.py`'s `NewFileTrigger`) rather than polling: it waits, without
  occupying a worker slot, for the next new raw input file to appear (reusing
  `step2.check_ls_for_processing` unchanged as the "is there something yet" check), dispatches
  one processing DagRun per file found (in that calibration's own process DAG, below), and
  retriggers itself for the next cycle -- or, once the run ends, writes `runEnd.log` and stops.
  This needs Airflow's `triggerer` process running (`airflow_automation/airflow_demo/airflow_demo.sh start` launches it
  alongside the scheduler, dag-processor and API server).
- `ngt_perfile_process_{calibration}` -- one DAG **per calibration** (built from one shared
  factory, `_build_process_dag`), unlike `file_detector` above -- so each calibration's process
  runs/logs/Grid view are separately browsable/pausable in the Airflow UI. Triggered once per input file (deterministic `run_id`,
  so a file rediscovered across cycles safely dedupes via Airflow's own duplicate-run_id
  rejection instead of needing its own bookkeeping -- see `airflow_api.trigger_dag_run`'s
  docstring). Runs that one file through Step 2, Step 3, and Step 4 as a straight
  `step2_express >> step3_alca >> step4_harvest >> step4_upload` chain. Step 2 and Step 3 each
  act on only that one file; Step 4 is the deliberate exception -- it harvests/uploads from
  *every* ALCARECO file accumulated for the run so far, every time (this is `step4.py`'s own
  pre-existing "reprocess everything" behavior, reused unchanged -- more statistics genuinely
  improve the payload). The execution flow is still one DagRun per file; Step 4 just looks at
  more than its own triggering file's data.

The trade-off of this design: 5 DAG definitions for 3 calibrations and deferred waits instead of
30s polling -- at the cost of needing the triggerer process, and of a calibration whose files
never arrive for an OMS-visible run (e.g. only SiStripBad gets seeded for a given run number in
a test scenario) leaving that calibration's own file-detector DagRun parked in a deferred state
until the run-end grace period (or, if the run never ends, the give-up timer) expires, instead of
cheaply polling and giving up sooner -- inherited from `oms.find_new_run`/`check_ls_for_processing`
unchanged.

Quick start (assumes `airflow_automation/airflow_demo/airflow_demo.sh setup` and `start` have already been run):

```bash
# --- per-file design ---
./airflow_automation/airflow_demo/airflow_demo.sh unpause-perfile                          # unpause all 5 DAGs (no calibration arg)
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml
# watch ngt_perfile_run_detector -> ngt_perfile_file_detector -> ngt_perfile_process_ecalpedestals
./airflow_automation/airflow_demo/airflow_demo.sh pause-perfile

```

**`airflow_automation/airflow_dags/ngt_dags_watch.py`**: 4 DAGs, **zero cron** -- every asset
static, driven by `AssetWatcher`s.

- `Asset("ngt://runs")` with a `RunWatcherTrigger` -- an event-driven `BaseEventTrigger` that
  runs continuously in the `triggerer` (while `ngt_watch_run_primer` is unpaused), polls OMS
  calibration-agnostically, and yields one event per new run.
- `ngt_watch_run_primer` (`schedule=[Asset("ngt://runs")]`) -- calls `step2.find_new_run` per
  calibration to create the working dirs the file watchers gate on. (An `AssetWatcher` can't be
  *spawned* by an event; the file watchers are always running, just idle until a run dir exists.)
- `Asset("ngt://files/<cal>")` ×3 with a `LumisectionFileWatcherTrigger` -- watches that
  calibration's active run dir and yields **one event per file**; writes `runEnd.log` on
  run-end. Dedup watermarks (seen runs / emitted files) live in each Asset's `AssetStateStore`.
- `ngt_watch_process_<cal>` (`schedule=[Asset("ngt://files/<cal>")]`) ×3 -- byte-for-byte
  `ngt_perfile_process_<cal>` (shared `_perfile_process.py` callables).

```bash
# --- AssetWatcher design (pause the others first -- shared run-dir dedup) ---
./airflow_automation/airflow_demo/airflow_demo.sh pause-perfile
./airflow_automation/airflow_demo/airflow_demo.sh unpause-watch            # also starts the 4 AssetWatchers in the triggerer
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml
# watch Asset ngt://runs -> ngt_watch_run_primer -> Asset ngt://files/ecalpedestals
#       -> ngt_watch_process_ecalpedestals   (all 4 assets show in the UI Assets view)
./airflow_automation/airflow_demo/airflow_demo.sh pause-watch              # stops the watchers
```

For a paced, narrated walkthrough instead (including a forced-failure/retry demonstration), use
`./airflow_automation/airflow_demo/perfile_interactive_demo.sh` or
`./airflow_automation/airflow_demo/watch_interactive_demo.sh`.

`tests/airflow_designs/test_dags_per_file.py` and `tests/airflow_designs/test_dags_watch.py` cover
both designs (see "Running the tests" above).

## Nota Bene
There are quite a lot of issues remaining, still. The FSM-based version this replaced was the
"functioning" version used for the demonstrator in the 2025 data taking; this Airflow-based
version (migrated from its original Airflow 2.10.5 implementation to Airflow 3.3.1) is the
planned improvement for 2026 data-taking, built and tested offline as described above. Actual
rollout onto the P5 calibration nodes is separate future work -- see "Deploying on the
calibration nodes".

## Public Presentations

- **Musich, M.** et al. (2025) *Task 3.4: Optimal Calibrations for the CMS High-Level Trigger*. Next Generation Triggers 2nd Technical Workshop, CERN, 21 November 2025. [DOI](https://doi.org/10.17181/gzvw9-t3379).

- **Zarucki, M.** (2026). *Demonstrating the Processing Chain for the Next Generation Triggers in the CMS Experiment*. 28th Conference on Computing in High Energy and Nuclear Physics (CHEP 2026), CMS, CERN. [DOI](https://doi.org/10.17181/txk7r-fsd29).

- **Prendi, J.** (2026). *Conceptual Design and Operation of the Calibration Loop for the Next Generation Triggers in the CMS Experiment*. 28th Conference on Computing in High Energy and Nuclear Physics (CHEP 2026), CERN, 28 May 2026. [DOI](https://doi.org/10.17181/rv6ad-zpy87).

