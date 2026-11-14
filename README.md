# The NGT Demonstrator Calibration Leg

## Overview
This directory implements the calibration leg workflow of our NGT demonstrator. It is designed to run the calibration loop on either of our calibration nodes (`ngtcalfu-c2b05-43-01.cms` and `ngtcalfu-c2b05-44-01.cms`) for the NGT demonstrator, through monitoring ongoing collisions at CMS and rederiving calibrations (for now only EcalPedestals and SiStrip Bad Components) up to the upload to the conditions database of CMS. Each calibration loop is implemented as a finite state machine (FSM) that monitors input data, processes it, and produces the output for the next step:

- **Step 2**: Monitors OMS for new runs, processes raw files files from EOS, produces (RE)RECO files
- **Step 3**: Monitors for step 2 output files, merges them, produces ALCARECO root files
- **Step 4**: Monitors for step 3 output files, performs harvesting, produces and uploads final payload to condDB to be consumed by the NGT Global Tag

The same processing logic is also extracted into the stateless `ngt_calibration_loop` package
(`ngt_calibration_loop/step{2,3,4}.py`), which an **Apache Airflow** prototype
(`airflow_automation/airflow_dags/`) orchestrates instead of the FSMs' `while True` polling
loops. See "Why Airflow" below for the motivation. The batching/latching *logic* is unchanged
from the FSMs, only how it's scheduled and retried.

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
│ - Output: {calibration}.db          │
└─────────────────────────────────────┘
    ↓
Automatic Database Upload
```
## tmux quick start
To understand exactly what goes into each step, please refer at the `Setting up and running` section. This section assumes, you just want to relaunch scripts after new developments that you want to run on and no tmux sessions running anymore (`tmux kill-server`). Make sure you are in the correct directory and that `cmsenv` is set. To launch all three tmux sessions at the same time, it just needs the input argument of which calibration one wants to launch, so simply:
```
./tmux_launch.sh -c SiStripBad # or -c Beamspot or -c EcalPedestals
```
This will launch the tmux sessions and make the respective script run inside of it. 



## Setting up and running

The `transitions` package and the `omsAPI` folder within the [oms-api-client](https://gitlab.cern.ch/cmsoms/oms-api-client). Step 2 is dependent on a `cron` job that is resetting the credentials every 12 hours via keytab:
```
[sakura@ngtcalfu-c2b05-44-01 ~]$ crontab -l
0 */12 * * * env KRB5CCNAME=FILE:/tmp/krb5cc_sakura_static /usr/bin/kinit sakura@CERN.CH -k -t /nfshome0/sakura/.globus/sakura.keytab >> /nfshome0/sakura/.globus/kinit_cron.log 2>&1
```
and this allows us to access the files on eos from our machines, this was also detailed in [this issue](https://github.com/cms-ngt-hlt/NGTCalibrationLoop/issues/14#issuecomment-3992441495).

All was launched in a tmux session, and all three steps can be run through the `sakura` user. Check whether the correct kerberos ticket was correctly generated via `klist`.

Preparing to launch the step 2 script requires:
```bash
sudo -u sakura -i
tmux new -s CalibrationLoop2
source /opt/offline/cmsset_default.sh
cmsrel CMSSW_16_0_3
cd CMSSW_16_0_3/src
cmsenv
git clone git@github.com:cms-ngt-hlt/NGTCalibrationLoop.git
git clone git@github.com:pytransitions/transitions.git
cd transitions
python3 setup.py install --user
cd ../NGTCalibrationLoop
git clone ssh://git@gitlab.cern.ch:7999/cmsoms/oms-api-client.git
cp -r oms-api-client/omsapi .
mkdir -p /tmp/ngt/
chmod g+ws /tmp/ngt/
python3 NGTLoopStep2.py -c EcalPedestals  # or SiStripBad
```
then quit the tmux session either via keyboard combination or simply closing the terminal. 

For step 3:
```bash
sudo -u sakura -i
tmux new -s CalibrationLoop3 # make sure to start the tmux session from the sakura account, s.t. the other from the group can also have access to it.
source /opt/offline/cmsset_default.sh
cmsrel CMSSW_16_0_3
cd CMSSW_16_0_3/src/NGTCalibrationLoop
cmsenv
python3 NGTLoopStep3.py -c EcalPedestals  # or SiStripBad
tmux detach
```
For step 4, we simply do:
```bash
sudo -u sakura -i
tmux new -s CalibrationLoop4 # make sure to start the tmux session from the sakura account, s.t. the other from the group can also have access to it.
source /opt/offline/cmsset_default.sh
cd CMSSW_16_0_3/src/NGTCalibrationLoop
cmsenv
python3 NGTLoopStep4.py -c EcalPedestals # or SiStripBad
tmux detach
```

One can check what tmux sessions are running and go back to a session through
```
tmux list-sessions
tmux attach -t 0
```

All steps may alternatively be used with `-c SiStripBad`, these are the only calibration workflows configured for now. They must all be running at the same time to guranatee timely uploads of conditions to the database.


## Every loop
### Step 2 loop

`NGTLoopStep2.py` continuously queries OMS for collisions run that started within the last 8 hours (which is the time we have to rederive calibrations and the time for which we buffer). Once a suitable run is found, a working directory is created where we start processing the RAW files available on EOS. The jobs are launched `cmsDriver.py` command. The possible states within this loop are:

- **NotRunning** - Waiting for a new collisions run to process
- **WaitingForLS** - Monitoring for new lumi sections (LS)
- **CheckingLSForProcess** - Evaluating available LS for processing
- **PreparingLS** - Preparing a batch of LS for job submission
- **PreparingFinalLS** - Preparing the final batch when run ends
- **PreparingExpressJobs** - Creating job scripts
- **LaunchingExpressJobs** - Submitting jobs to process LS
- **CleanupState** - Finalizing run processing

This step also maintains separate log files for different types of logs --- a complete collection of all can be found in `/tmp/ngt/NGTLoopStep2_ALL.log`, to monitor activity, one can do `tail -f /tmp/ngt/NGTLoopStep2_ALL.log`. This step has to be run on a personal cmsusr account due to access needed to EOS.

### Step 3 + 4 loop

Step 3 loop processes the output root files of step2 in order to produce the `ALCARECO` files. The FSM is similar to the one of step 2 described above, used to submit the `ALCA` jobs. Step 3 can be run on either sakura or personal `cmsusr` account, it does not really matter here.

Step 4 loop takes all available ALCARECO files available at a given time that were produced from step 3 and performs the ALCAHARVESTING and the eventual upload of the payload to condDB. It re-harvests files as with time we gain more statistics but we still would like to upload conditions payloads as soon as we have them. Step 4 must be run on the sakura account for the eventual upload to the conditions database.

### Complete Directory Structure
```
/tmp/ngt/
├── calibrationYAML/
│   ├── SiStripBad.yaml
│   └── EcalPedestals.yaml
├── NGTLoopStep2_ALL.log        # Step 2: All log levels
├── NGTLoopStep2_INFO.log       # Step 2: Info only
├── NGTLoopStep2_WARNING.log    # Step 2: Warnings only
├── NGTLoopStep2_ERROR.log      # Step 2: Errors only
├── NGTLoopStep2_CRITICAL.log   # Step 2: Critical only
├── NGTLoopStep3_ALL.log        # Step 3: All log levels
├── ...
├── NGTLoopStep4_ALL.log        # Step 4: All log levels
├── ...
└── run{run_number}/
    ├── runStart.log            # Created by Step 2: ISO timestamp
    ├── runEnd.log              # Created by Step 2: Signals completion
    │
    ├── CMSSW_X_Y_Z/            # CMSSW release (created by Step 2 jobs)
    │
    ├── cmsDriver_*.sh          # Step 2: Job scripts (temporary)
    ├── run*_LS*_step2.py       # Step 2: CMSSW Python configs
    ├── run*_LS*_step2.log      # Step 2: Job logs
    ├── run*_LS*_step2.root     # Step 2: RECO output files
    ├── run*_LS*_step2_job.txt  # Step 2: Witness files
    │
    ├── allLSProcessed.log              # Step 2: List of processed LS files
    ├── expectedOutputs.log             # Step 2: Expected Step 2 outputs
    ├── allStep2FilesProcessed.log      # Step 3: List of processed Step 2 files
    ├── allStep3FilesProcessed.log      # Step 4: List of processed Step 3 files
    │
    ├── alcaPromptJob000/               # Step 3: First ALCA job
    │   ├── ALCAOUTPUT.sh
    │   ├── run*_step3.py
    │   ├── stdout.log
    │   ├── stderr.log
    │   ├── PromptCalibProdEcalPedestals.root
    │   └── step3_job.txt               # Witness file
    │
    ├── alcaPromptJob001/               # Step 3: Second ALCA job
    │   └── ...
    │
    ├── harvestJob000/                  # Step 4: First harvesting job
    │   ├── HARVESTING.sh
    │   ├── run*_step4.py
    │   ├── stdout.log
    │   ├── stderr.log
    │   ├── metadata.json
    │   ├── promptCalibConditions.db
    │   └── EcalPedestals.db            # Final output
    │
    └── harvestJob001/                  # Step 4: Updated harvesting
        └── ...
```

## Running the FSM logic locally / tests

The three loops don't need CMSSW, EOS, OMS, or a real conditions DB to exercise their
state-machine logic. `/data/ngt`, `/tmp/ngt`, and `/nfshome0/sakura` are configurable
via `DATA_BASE_PATH`, `LOG_BASE_PATH`, and `COND_AUTH_PATH` in `ngtParameters.jsn`
(they default to those same production paths, so deployment behavior is unchanged),
and each script now guards its argument parsing / main loop behind
`if __name__ == "__main__":`, so `NGTLoopStep2/3/4.py` can be imported without
launching anything.

To set up a local environment and run the test suite:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests in `tests/` mock every external dependency:
- OMS REST API -> `tests/stubs/omsapi` (an in-memory fake of the query-builder
  interface `NGTLoopStep2.py` uses; the real `oms-api-client` isn't installed for
  tests, only for production deploys per the instructions above)
- `edmFileUtil` / `xrdfs` (EOS access) -> `tests/support/fake_subprocess.FakeEOS`
- `cmsDriver.py` / `cmsRun` / `uploadConditions.py` -> never actually invoked;
  `subprocess.Popen` is replaced with a recorder, so "launching a job" just records
  what would have run
- filesystem paths -> redirected into a pytest `tmp_path` via the
  `NGT_PARAMETERS_PATH` / `NGT_CALIBRATION_YAML_DIR` env vars each script reads

No network access, CMSSW, or CERN-internal hosts are required to run the suite.

## Running a live demo of the loops (no CERN infra needed)

For watching the actual FSM processes run -- not just pytest -- `scenario-player/live_demo.sh`
launches real `NGTLoopStep2/3/4.py` processes (one per tmux session) against a fake
OMS/EOS/CMSSW toolchain in `scenario-player/bin/` (fake `cmsDriver.py`, `cmsRun`, `edmFileUtil`,
`xrdfs`, `cmsrel`, `cmsenv`, `uploadConditions.py`). Everything runs for real --
real state transitions, real generated job scripts, dummy output files flowing from
Step 2 through Step 3 to a fake Step 4 upload -- entirely offline.

Quick start example:
```bash
./scenario-player/live_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml --interactive --monitor
./scenario-player/live_demo.sh stop-all EcalPedestals
```

`--interactive`
waits for Enter before each step (printing the command it is about to run), and
`--monitor` reports every file the loops produce as it appears.

That one command is enough from a cold start (after creating a venv and installing requirements as shown above): `play` sources the .venv, creates `$NGT_DEV_HOME` if it isn't
there and launches Step 2/3/4 for the calibrations that scenario feeds (asking the scenario player
which those are), then passes everything after it straight to `scenario_player.py` -- see
[Scripting a timed sequence of runs/lumisections](#scripting-a-timed-sequence-of-runslumisections)
below for the scenario format and the rest of the flags.

Every shipped scenario also has an `expect:` block, which `play` checks once the uploads settle
(see [Validating the result](#validating-the-result-did-the-scenario-actually-pass) below);
`--no-validate` skips it.

Run `./scenario-player/live_demo.sh` with no arguments for the full command list. All state
lives under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory inside
this repo); `./scenario-player/live_demo.sh reset` stops the loops and deletes it, which resets the demo
environment completely.

You can run the demo manually without a scripted scenario, e.g.:

```bash
./scenario-player/live_demo.sh setup                                  # create ./demo-env scratch env
./scenario-player/live_demo.sh start-all EcalPedestals                 # launch all 3 steps in tmux
./scenario-player/live_demo.sh seed-run EcalPedestals 398600 --ls 51 52  # latch a fake live run
tmux attach -t NGTDemo_<instance>_2_EcalPedestals           # watch it react (Ctrl-b d to detach)
./scenario-player/live_demo.sh add-ls EcalPedestals 398600 53           # simulate a new lumisection arriving
./scenario-player/live_demo.sh end-run 398600                           # simulate the run ending
./scenario-player/live_demo.sh status                                   # list running sessions
./scenario-player/live_demo.sh stop-all EcalPedestals                   # tear down
```

Three settings are overridable via environment variables:

| Variable                 | Default                              | Meaning                          |
|---------------------------|--------------------------------------|-----------------------------------|
| `NGT_DEV_HOME`             | `./demo-env`                         | scratch state directory           |
| `NGT_LOOP_SLEEP_SECONDS`   | `5` (production default: `60`)       | Step 3/4 poll interval, seconds   |
| `NGT_INSTANCE`             | checkout dir name + a hash of its path and `NGT_DEV_HOME` | names this demo's tmux sessions |

e.g. `NGT_DEV_HOME=/tmp/ngt-demo ./scenario-player/live_demo.sh setup`. `NGT_DEV_HOME` is read
at `setup` time and baked into `$NGT_DEV_HOME/ngtParameters.jsn`, so re-run `setup`
after changing it. `NGT_LOOP_SLEEP_SECONDS` is read fresh each time a loop is
started (`start2` / `start3` / `start4` / `start-all`).

The loops' tmux sessions are named `NGTDemo_<NGT_INSTANCE>_<step>_<calibration>` (`start-all`
and `status` print them), and `status`, `stop-all` and `reset` only see their own. The default
`NGT_INSTANCE` follows the checkout and `NGT_DEV_HOME`, so two checkouts -- or two `NGT_DEV_HOME`s
 of one -- run their demos side by side. Use the same `NGT_DEV_HOME` in every command of one demo.

## The `ngt_calibration_loop` library

The decision logic of the three loops -- OMS/EOS querying, run latching, lumisection/file
batching and timeouts, cmsDriver script preparation -- is also available as plain functions with
no orchestration dependency in `ngt_calibration_loop/` (`step2.py`/`step3.py`/`step4.py` hold
each step's logic; `config.py`, `oms.py`, `eos.py` and `shell.py` are shared helpers). They keep
their state on disk instead of in memory, and Step 3/4 job directories are named from a content
hash of their input files (`alcaPromptJob_<hash>`, `harvestJob_<hash>`) instead of a counter, so
a retried job reproduces the same directory instead of double-submitting.
`shell.run_job_script` is the one seam that actually launches a job script, blocking until it
completes (the FSM scripts' `subprocess.Popen(...)` never checked the result). `oms.run_end_time`
asks OMS directly for a run's end time. Three give-up timers (`step_2_config.maxLatchTimeHours`,
`step_3_config`/`step_4_config.timeoutSeconds`) can be overridden by optional calibrationYAML keys
-- absent in the production calibrationYAML, so production behavior is unaffected.
`original_fsm_only/` holds the few functions only the original FSM orchestration used (the Step
3/4 run-directory scan, Step 3's witness-file batching decision) -- no Airflow design calls them,
they're kept, tested, but out of the step modules the DAGs import.

### Tests of the library and the simulator

The same mocks as above apply, except that job launches are intercepted one level down:

- `tests/fsm-loops/test_step{2,3,4}_lib.py` -- exercise `ngt_calibration_loop`'s functions directly: run
  latching, batching thresholds, timeout/run-ended escape hatches, job prep/launch, and that a
  failed launch raises so a caller can retry. `ngt_calibration_loop.shell.run_job_script` is
  replaced by the `job_runner` fixture, a recorder that can also be told to raise on demand
  (`job_runner.queue_failure()`).
- `tests/ngt-calib-lib/test_original_fsm_only.py` -- the same style of tests for
  `ngt_calibration_loop/original_fsm_only/` (run-directory scan, Step 3's witness-file batching
  decision).
- `tests/ngt-calib-lib/test_run_step.py` -- `ngt_calibration_loop/run_step.py`, one step for one input
  file end to end: each step taking the previous one's result from the run's working directory,
  "nothing to do", and its command line.
- `tests/scenario-player/test_scenario_player.py` -- `scenario-player/scenario_player.py`'s timeline-building
  (ordering, concurrent-run merging) and playback (event firing order/arguments), with
  `scenario-player/seed.py`'s functions monkeypatched out -- no filesystem/`$NGT_DEV_HOME` access
  needed.
- `tests/scenario-player/test_faults.py`, `tests/ngt-calib-lib/test_job_faults.py`, `tests/ngt-calib-lib/test_oms_faults.py` -- the
  declarative fault vocabulary (see "Fault injection" below).
- `tests/scenario-player/test_fake_toolchain_provenance.py`, `tests/scenario-player/test_fake_conddb.py`,
  `tests/scenario-player/test_scenario_expectations.py` -- the provenance chain through the fake toolchain (run as
  real subprocesses), the conditions-DB ledger and the `expect:` matching rules (see "Validating
  the result" below).
- `tests/scenario-player/test_fake_toolchain_isolation.py`, `tests/scenario-player/test_sim_env.py` -- what `sim_env.sh`'s
  generated `env.sh` gives a shell: the fake toolchain winning over an exported `cmsenv`, and the
  fault-injection paths (with a real bash).
- `tests/fsm-loops/test_scenarios.py` -- the slow end-to-end runs, deselected by default
  (`pytest -m scenario`; CI's `scenario-test` job): play `basic_ecal_pedestals.yaml` and
  `fault_injection_demo.yaml` through `live_demo.sh` against the real FSM loops and check what they
  left behind, the payloads that reached the conditions DB included.
- `tests/airflow-ngt/test_dags_watch.py` -- DagBag import-error/structure checks for the
  AssetWatcher design, the two watcher triggers' async `run()` driven to completion against a
  fake `AssetStateStore` (plain `asyncio.run()` calls -- no `pytest-asyncio` needed), and direct
  unit tests of each task's `python_callable`, including a chain smoke test that drives one file
  through all four of them. No live Airflow scheduler/DB/API server needed. Guarded by
  `pytest.importorskip("airflow")`, so it's automatically skipped wherever `apache-airflow` isn't
  installed (e.g. a plain Windows `.venv`) and the rest of the suite still runs. Where Airflow
  *is* installed, run `source airflow_automation/airflow_demo/airflow_env.sh` first so
  `import airflow` picks up the right `AIRFLOW_HOME`/DB config.
- `tests/airflow-ngt/test_airflow_containers.py` -- the container deployments without a container runtime:
  that the manifest stays plain Kubernetes and meets the `restricted` Pod Security level, with read-only root
  filesystems, that the Compose files run as an arbitrary user, that the one-pod UI is read-only without its login, and how `airflow_demo.sh` behaves inside a container. Its slow
  counterpart, `test_containers_live.py`, is deselected by default (`pytest -m container`, needs Docker): it plays
  a scenario against both deployments.
- `tests/airflow-ngt/test_airflow_openshift.py` -- the OpenShift example without a cluster: that what `oc apply -k`
  creates asks for nothing the restricted SCC would reject, fits a project's default quota, and that its Route
  stays CERN-only. The tests that render the overlay skip without `kubectl`.
- `tests/airflow-ngt/test_airflow_daq_dev.py` -- the CMS DAQ `daq-dev` example without a cluster: that what
  `kubectl apply -k` creates keeps to the rules its tenant enforces (registries, namespace prefix, Ingress class and
  hostnames, the LimitRange), and that the pod passes the checks the cluster's Kyverno policies report. They skip
  without `kubectl`.
- `tests/airflow-ngt/test_airflow_multipod_helm.py` -- the multi-pod deployment without a cluster: that the chart, its
  own templates and the post-renderer fit together, that every pod runs the strictest way a cluster can run it, and
  that the setup script sets up once. The chart's half needs Helm 3.19+ and Airflow's chart, which
  `tests/airflow-ngt/helm_chart_fixtures.py` downloads once into pytest's cache (or takes from `$NGT_AIRFLOW_CHART`).
- `tests/airflow-ngt/test_airflow_daq_dev_multipod.py` -- the same on `daq-dev`: its tenant's rules on every pod and
  volume, and a host of its own for each namespace.

## The offline simulator (`scenario-player/`)

`scenario-player/` holds everything needed to simulate a live run without CERN infrastructure:
`sim_env.sh` (scratch environment + fake toolchain bootstrap), `seed.py` and `scenario_player.py`
(driving/replaying fake runs, with `scenarios/*.yaml`), `faults.py` (the shared fault-injection
vocabulary, see "Fault injection" below), `fake_conddb.py` and `expectations.py` (the dummy
conditions DB and the `expect:` blocks, see "Validating the result" below) and `bin/` (the fake
`cmsRun`/`cmsDriver.py`/`edmFileUtil`/`xrdfs`/... toolchain). None of it imports or depends on how
`NGTLoopStep2/3/4.py` are launched, so it can be pointed at the same `$NGT_DEV_HOME` regardless.
It is not a Python package (hence the hyphen): its `.py` files are
top-level modules found via `sys.path` (`tests/conftest.py`, and `PYTHONPATH` from `sim_env.sh`).

### Scripting a timed sequence of runs/lumisections

`scenario-player/scenario_player.py` plays
back a whole timeline from a declarative YAML file, e.g.:

```yaml
runs:
  - calibrations: [EcalPedestals, SiStripBad]
    run: 398800
    start_offset: 0
    lumisections:
      - {ls: 1, after: 0}
      - {ls: 2, after: 15}
    end_after: 20
```    

A scenario lists one or more runs with a start offset and a sequence of lumisections separated
by delays; events from multiple runs merge into one global timeline. Runs may not overlap (only
one CMS-wide DAQ run is active at once), but a run's `calibrations` list can share its
lumisections across several calibrations at once -- see `scenario-player/scenario_player.py`'s
module docstring for the full format and `scenario-player/scenarios/*.yaml` for worked examples.
Run numbers must be exactly six digits: the fake `edmFileUtil` parses `run<RUN6DIGIT>_ls<LS>`
names and skips anything else.

Example executions:

```bash
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml            # real-time
python3 scenario-player/scenario_player.py scenario-player/scenarios/concurrent_multi_calibration.yaml --speed 5
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml --interactive --monitor
```

`./scenario-player/live_demo.sh play <scenario.yaml> [options]` is the same thing with the
venv activated, the scratch environment set up and the scenario's loops started for you --
every argument after `play` is passed straight through.

The player itself starts no loops -- `./scenario-player/live_demo.sh play` does that for you,
and `setup` / `start-all <calibration>` are still there to drive by hand. `--monitor` reports
*new* paths only, so a `demo-env` left over from an earlier scenario won't flood the output,
and file *contents* are still yours to look at (`tail` a `stdout.log`, or
`./scenario-player/live_demo.sh logs <calibration> 2`).

This calls `seed.py`'s functions directly, nothing more -- the same engine-agnostic layer
`seed.py` itself is.

### Fault injection

OMS (`connection_error`, `timeout`, `http_error` with a status, `malformed_json`), `cmsRun` and
`uploadConditions.py` (`exit_code`) can be made to fail on purpose; each prints a one-line
`(FAKE) injected <target> fault: ...` message. The vocabulary is `scenario-player/faults.py`. pytest arms a fault
in-process with the `inject_fault` fixture (an OMS one through `tests/stubs/omsapi`'s `set_failure()`); a live run
arms faults by writing `$NGT_DEV_HOME/faults/<target>.json`, which the fakes consume -- from a scenario's top-level
`faults:` list (see `scenario-player/scenarios/fault_injection_demo.yaml`, or
`oms_outage_demo.yaml`, which takes OMS down just as a run starts) or by hand:

```bash
python3 scenario-player/seed.py arm-fault --target oms --mode connection_error --times 2
python3 scenario-player/seed.py arm-fault --target cmsrun --mode exit_code --exit-code 139 \
    --calibration EcalPedestals --step step2
python3 scenario-player/seed.py list-faults
python3 scenario-player/seed.py clear-faults
```

`$NGT_DEV_HOME/env.sh` points the fakes at those files (`NGT_FAULTS_DIR`), so the faults reach the
FSM loops `live_demo.sh` starts. The FSM scripts never retry or check a job's exit code: a faulted
`cmsRun` simply loses that job's output, and the lumisections it covered never make it into a
payload. A failed upload is made good only by a later harvest of the same run -- each one takes
everything the run has produced so far, the last when the run ends.

Tests: `tests/scenario-player/test_faults.py`, `test_omsapi_faults.py`, `test_fake_job_faults.py`
and `test_sim_env.py`; `tests/ngt-calib-lib/test_oms_faults.py` and `test_job_faults.py` aim the
vocabulary at the step libraries.

### Validating the result: did the scenario actually pass?

A scenario can declare what should reach the conditions DB, in a top-level `expect:` block: after
playback the player waits for the uploads to settle, checks them, and exits nonzero if they don't
match.

```bash
python3 scenario-player/scenario_player.py scenario-player/scenarios/basic_ecal_pedestals.yaml
echo "exit=$?"    # 0 = expectations held, 1 = they didn't, 2 = the scenario is malformed
```

For that, the fakes carry identifiers along the real data path. Each fake `cmsRun` output is a
*provenance manifest* naming the calibration/run/lumisections its inputs carried -- rooted in the
`run<RUN>_ls<LS>` names of the raw files `seed.py` creates -- and the fake `uploadConditions.py`
appends what the `.db` it is handed covers to a dummy conditions DB,
`$NGT_DEV_HOME/conddb/payloads.jsonl` (one JSON object per upload, under `flock`):

```json
{"seq": 1, "uploaded_at": "2026-10-06T12:34:56Z", "calibration": "EcalPedestals", "run": 398600,
 "lumisections": [51, 52, 53], "n_input_files": 3, "db_file": "NGTCalibEcalPedestals.db",
 "job_dir": ".../EcalPedestals/run398600/harvestJob000"}
```

So a harvest fed the wrong files uploads visibly wrong lumisections. Only the uploads made during
the play are checked; earlier plays' stay in the ledger until `reset`.

An `expect:` block lists the expected *sequence* of uploads; every upload must be matched by an
entry or allowed by an `allow_extra` category:

```yaml
expect:
  settle_timeout: 600          # cap on waiting for uploads to arrive
  quiet_for: 45                # settled once no new upload lands for this long
  allow_extra: {before: true}  # scalar true/false sets all four categories at once
  payloads:
    - {calibration: EcalPedestals, run: 398600, lumisections: 51-53}
```

| Flag | The question it answers | Default |
| --- | --- | --- |
| `allow_missing_lumisections` / `allow_extra_lumisections` | does a matched upload carry **the right lumisections**? | both `false` -> exact |
| `allow_extra.{payloads,before,between,after}` | may the ledger hold **uploads nobody listed**, and where? | `false` |
| `allow_duplicates` | may two uploads carry an **identical** lumisection set? | `false` |
| `ordered` | must the listed entries land in the listed **order**? | `false` |

- `lumisections:` is a range, `51-53`, or a list, `[51, 53]`. The flags belong to the whole block:
  one written inside a `payloads` entry is an error.
- `payloads: []` with `allow_extra: false` asserts that nothing reached the conditions DB. A failed
  upload, injected or not, records nothing; an upload covering no lumisections always fails the check.
- `before` and `after` are separate knobs: Step 4 re-harvests everything available each cycle, so a
  run climbs through growing payloads before the final one, while one *after* it is suspect. A
  duplicate is an *identical* lumisection set twice, not just a second upload.
- The check waits for the uploads to go quiet, not for the expectations to hold -- that is what lets
  `after: false` catch a late upload. Still arriving at `settle_timeout` means "timed out".
- The shipped scenarios list each run's final payload and allow the rest:
  `allow_extra: {before: true, after: true}` and `allow_duplicates: true`, since these loops'
  Step 4 uploads the final payload again when the run ends.

`--no-validate` plays a scenario ignoring its block; `--dry-run` prints the block with the timeline.

### Real files, real CMSSW

A lumisection in a scenario can name a real file to stand for it: `{ls: 1246, after: 0, file: <path>}`
(`$VARS` expanded, checked before anything plays). The file is linked into the fake EOS directory as
`run<RUN>_ls<LS>_<its name>`, so the fake `edmFileUtil` still reads the run and LS off the name. The
fake EOS is a directory, so `sim_env.sh` sets `ngtParameters.jsn`'s `INPUT_URL_PREFIX` to `file:`. Step
2's job then reads the file directly rather than through `root://eoscms.cern.ch/`.

A top-level `calibration_config:` is merged into `$NGT_DEV_HOME/calibrationYAML` when playback starts,
until the next reset: maps merge, anything else replaces. A real run of the `EcalPedestals` steps uses
it to lower the pedestal harvester's threshold for an input of a few lumisections.

What processes the file depends on where the steps run, not on the scenario:
- **With the fake toolchain,** the steps are fake as usual.
- **In pods of a CMSSW image,** the steps are real: the multi-pod deployment with `values-step-pods.yaml` and a
  cluster's CMSSW values.

**The upload stays the fake one.** It records a real `.db` too, by its job directory, as
`"payload": "real"` with its size and checksum: its lumisections aren't in it. It refuses an empty one,
which a harvester leaves when it found too little.

**Checking a real run:** an `expect:` entry without `lumisections` matches its calibration and run alone.
Such scenarios live in `scenario-player/scenarios/real-cmssw/`, away from the ones every machine can play.

Tests: `tests/scenario-player/test_fake_toolchain_provenance.py`, `test_fake_conddb.py` and
`test_scenario_expectations.py`; `tests/fsm-loops/test_scenarios.py` checks the blocks of
`basic_ecal_pedestals.yaml` and `fault_injection_demo.yaml` against the real loops.

### Resetting the simulator's state and the give-up timers

All simulator state lives under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory
inside this repo), including the `conddb/` upload ledger:

```bash
./scenario-player/sim_env.sh reset   # wipes $NGT_DEV_HOME entirely
./scenario-player/sim_env.sh setup   # recreates it (needs an active venv -- see note below)
```

`setup`'s `pip install -e tests/stubs` step needs **some** Python venv active first -- without
one, Debian/Ubuntu's system Python refuses the install outright (PEP 668,
`error: externally-managed-environment`); forgetting this fails fast with a clear message
telling you to `source .../activate` first.

**Caution**: `./scenario-player/sim_env.sh reset` deletes `$NGT_DEV_HOME` unconditionally, with
no awareness of whether a process is currently running against it. Doing this while an engine
has in-flight work polling paths under `$NGT_DEV_HOME` can leave that work stuck, and silently
orphans the engine processes' own console logs (they keep writing to the now-unlinked files).
Stop whatever is running against the environment first (with Airflow, use
`./airflow_automation/airflow_demo/airflow_demo.sh reset-scenario` instead -- see "Resetting state"
under "Running a live demo with Airflow").

`setup` also patches the give-up timers of the scratch calibrationYAML copies (see "The
`ngt_calibration_loop` library"):

| Variable                          | Default                          | Meaning                          |
|-------------------------------------|-----------------------------------|-----------------------------------|
| `NGT_TEST_MAX_LATCH_TIME_HOURS`      | `0.25` (production default: `8`) | how long a calibration keeps waiting on a run before giving up, hours |
| `NGT_TEST_STEP_TIMEOUT_SECONDS`      | `900` (production default: `28800`) | same, for Step 3/Step 4's own give-up timers, seconds |

Why they exist: step2/3/4 each wait up to several *production* hours (8h each) before giving
up on a run/calibration that never reconciles -- correct for real OMS/EOS, but not something a
live-test scenario should ever have to wait out (a scenario that seeds files for only one
calibration still latches all three onto the same OMS-visible run, so the other two would
otherwise wait for hours). The defaults (15 min) are deliberately well above any realistic
scenario duration -- shortening them further risks a still-live, still-progressing run's own
workflow getting force-finalized mid-scenario. Override either one to test the
give-up-and-finalize path itself, or to give a deliberately long-running scenario more headroom.

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
its own retry policy. The DAG structure built on this -- an event-driven `AssetWatcher`
design, see "Airflow DAG design" below -- runs one input
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

**Airflow 3 note**: this design needs no cross-DAG coordination from task code at all --
hand-offs happen through Asset events, not by one DAG triggering another. That matters under
Airflow 3, which no longer permits `airflow.api.common.trigger_dag` or direct `DagRun`/
`TaskInstance` ORM queries from task code (what this repo did under Airflow 2 -- see its
"Upgrading to Airflow 3" docs on removed direct database access). The demo instance's own auth
uses the Simple Auth Manager's zero-credential admin-token mode, appropriate for this offline
single-user dev instance specifically (see `airflow_automation/airflow_demo/airflow_env.sh`).

**Scope note**: this is built and fully tested against the offline fake OMS/EOS/CMSSW
toolchain (`scenario-player/bin/`, `tests/stubs/`, `scenario-player/seed.py`) -- the same one the project's pytest
suite and live demo already used before this migration. Deploying it onto the real CERN P5
calibration nodes (real OMS, real EOS/kerberos, real CMSSW, real condDB) is a separate,
not-yet-done step; see "Deploying on the calibration nodes" below for what that would involve.
The FSM scripts above remain the "functioning" version used for the demonstrator in the 2025
data taking; this Airflow-based version (migrated from its original Airflow 2.10.5
implementation to Airflow 3.3.1) is the planned improvement for 2026 data-taking.

## Setting up and running Airflow

This is the setup on a bare host, for working on the DAGs; to run it in containers instead, with no
venv, Postgres or tmux on the host, see "Running Airflow in containers" below.

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

# 2. Postgres (one-time, needs sudo): just the role. It needs CREATEDB because each
#    checkout of this repo gets its own database (step 3), named distinctly from any
#    pre-existing Airflow 2.x install's -- see airflow_automation/airflow_demo/airflow_env.sh.
sudo apt-get install -y postgresql
sudo -u postgres psql -c "CREATE ROLE airflow_ngt WITH LOGIN CREATEDB PASSWORD 'airflow_ngt';"  # skip if it already exists
# (a role created earlier without CREATEDB: ALTER ROLE airflow_ngt CREATEDB;)

# 3. Initialize Airflow: create this checkout's database and migrate it
./airflow_automation/airflow_demo/airflow_demo.sh setup
#    -- by hand instead: `source airflow_automation/airflow_demo/airflow_env.sh` (AIRFLOW_HOME,
#    executor, DB, DAGs folder, auth), `createdb` the database it names, then `airflow db migrate`
```

**Running several checkouts side by side.** Everything that could collide between two clones of
this repo on one machine -- or two `$NGT_DEV_HOME`s of one -- defaults to a name derived from the
clone's directory and `$NGT_DEV_HOME` (`$NGT_INSTANCE`: the clone's basename plus a short hash of
its full path and `$NGT_DEV_HOME`): the Airflow home
(`~/airflow3-ngt-<instance>`), the Postgres database (`airflow3_ngt_<instance>`), the API
server/UI port (8100-8899, from the same hash), the two task-log server ports, the tmux session
names (`NGTAirflow_<instance>_*`) and `$NGT_DEV_HOME` (`./demo-env`). So two clones, or two
`$NGT_DEV_HOME`s of one, need no configuration to run isolated -- as long as every command of an
instance gets the same `$NGT_DEV_HOME`, or it is a different instance; `./airflow_automation/airflow_demo/airflow_demo.sh ui` prints a clone's
URL. Every one of those values is overridable -- from the environment, or persistently from
the gitignored `airflow_automation/airflow_demo/local_airflow_env.sh` (Airflow side) and
`scenario-player/local_sim_env.sh` (simulator side, read by `live_demo.sh` too), which are
sourced before the defaults are applied, so a value set there beats the environment
(`NGT_SKIP_LOCAL_ENV=1` skips both files; the test suite sets it). Copy the `.example` file
beside each to start; it lists every knob. The four Airflow processes get exactly the values
`airflow_demo.sh` resolved: left alone, a tmux session inherits the tmux *server's* environment
-- that of whichever shell first started the server, possibly another clone's `start` -- and
that clone's settings would beat these defaults. For the same reason the demo scripts start
the tmux server without any of these settings, so a shell you later open in it carries no
clone's identity.

No `airflow users create` step: `airflow_automation/airflow_demo/airflow_env.sh` sets
`AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True`, so Airflow 3's Simple Auth Manager (the
new default, replacing FAB) treats every request as admin with no login at all -- appropriate
for this offline, single-user dev/demo instance specifically (that auth manager is explicitly
documented as development/testing-only, which is exactly what this is), not something to carry
into a real deployment. For the same reason the UI listens on `127.0.0.1` only (`AIRFLOW__API__HOST`):
a browser on the Windows side of WSL still reaches it at `http://localhost:<port>`, but not at the WSL IP.

Why `pip install -e tests/stubs`: the fake `omsapi` package (see below) needs to be importable
by Airflow's scheduler subprocesses. `PYTHONPATH` set in the launching shell was not reliably
observed to reach those forked/spawned subprocesses in testing, so the stub is installed as a
real (tiny) package instead -- see `scenario-player/sim_env.sh`'s `sim_setup` for the note.

`airflow_automation/airflow_demo/airflow_demo.sh setup`/`start`/`stop`/`unpause` wrap steps 1-3 above and the day-to-day
process lifecycle -- see "Running a live demo with Airflow" below for the full walkthrough. Airflow 3 splits
DAG parsing out of the scheduler into its own mandatory `airflow dag-processor` process (the
scheduler no longer spawns it itself, unlike Airflow 2), so `start`/`stop`/`status` manage four
processes (scheduler, dag-processor, API server, triggerer) instead of the original two. The
triggerer hosts the AssetWatcher design's watchers -- see "Airflow DAG design" below -- but is
started unconditionally since it's harmless when unused.

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

## Running Airflow in containers

`airflow_automation/airflow_containers/` runs the same DAGs against the same fake toolchain from one
image, in three shapes. Its [README](airflow_automation/airflow_containers/README.md) is the tutorial.

- **Separate containers on one machine** (`compose/airflow_compose.sh`, Docker Compose): API server, scheduler,
  dag-processor and triggerer each in their own container, with Postgres and a one-shot init -- the layout
  Airflow's deployment docs describe, restarted and health-checked by the runtime. It takes the same commands
  and scenarios as `airflow_demo.sh`, which it runs inside a one-shot container for everything but
  the process management:
  ```bash
  ./airflow_automation/airflow_containers/compose/airflow_compose.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
  ```
- **One pod on Kubernetes** (`onepod/`): the quickest way to put the UI on a cluster. All the components in one
  container next to a Postgres sidecar, all state on `emptyDir`s. Anyone who can reach the UI can read it, and changing
  anything takes a generated Admin login (FAB's). Plain Kubernetes that meets the `restricted` Pod Security level, with
  read-only root filesystems; what
  depends on the cluster, such as how the image gets there and how the UI gets exposed, goes on top as a kustomize overlay.
- **On CERN's OpenShift** (`openshift/`): a worked example of such an overlay, with its own tutorial: an in-cluster
  image build and a Route reachable from the CERN network only. Not yet run on the real cluster.
- **On CMS DAQ's `daq-dev` cluster** (`daq-dev/`): another, with its own tutorial: images from the CMS Harbor, pushed
  through a SOCKS tunnel, and an Ingress reachable from the CMS network only. Deployed there on 2026-11-07.
- **Several pods on Kubernetes, with persistent storage** (`multipod-helm/`): one pod per Airflow component, as a chart
  with Airflow's own as its dependency. It adds Postgres, the fake data and the logs on persistent volumes, which
  `helm uninstall` keeps, so the history outlives any pod and a reinstall. Helm 4, with a post-renderer plugin for what
  Airflow's chart's values can't set; any namespace, several at once. With `values-step-pods.yaml`, each file's steps
  run in pods of their own (KubernetesPodOperator), of Airflow's image or another, such as a CMSSW one. On `daq-dev` as
  well (`daq-dev-multipod/`), with its own tutorial. Deployed there on 2026-11-13.

The image works as an arbitrary non-root user with no capabilities, the strictest way a cluster can run it, and the
Compose files run it that way. None of this changes what "Deploying on the calibration nodes" above says about the
real pipeline.

## Package layout

- `NGTLoopStep2/3/4.py` and `ngt_loop_config.py` -- the original FSM loop scripts (see "Every
  loop" above) and the config/logging helpers they share; `tests/fsm-loops/test_step{2,3,4}.py` cover them.
- `ngt_calibration_loop/` -- the processing logic (OMS/EOS querying, cmsDriver script
  preparation, batching decisions), as plain stateless functions with no Airflow dependency.
  `config.py`/`oms.py`/`eos.py`/`shell.py` are shared helpers; `step2.py`/`step3.py`/`step4.py`
  hold each step's logic. `shell.run_job_script` is the one seam that actually launches a job
  script, blocking until it completes -- this is what replaced the old `subprocess.Popen(...)`
  fire-and-forget calls. `original_fsm_only/` holds the few functions only the original
  FSM orchestration used (the Step 3/4 run-directory scan, Step 3's witness-file batching
  decision) -- no Airflow design calls them, they're kept, tested, but out of the step modules
  the DAGs import.
- `airflow_automation/airflow_dags/ngt_dags_watch.py` -- the AssetWatcher-driven DAG design:
  **all assets static, driven by `AssetWatcher`s, zero cron DAGs**. One `Asset("ngt://runs")`
  watched for new runs, three static `Asset("ngt://files/<cal>")` watched for that run's new
  files (one event per file); watermarks in each Asset's `AssetStateStore`. Its triggers live in
  `airflow_automation/airflow_dags/triggers.py`, and its per-step task callables in
  `airflow_automation/airflow_dags/_process_tasks.py` -- kept DAG-free so they stay
  independently unit-testable. Each calls one step of the library's
  `ngt_calibration_loop/run_step.py`, which can also run that step outside Airflow.
  See [`airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md`](airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md).
- `calibrationYAML/` -- per-calibration config (unchanged format from the original FSM setup).
- `tests/` -- see "Running the FSM logic locally / tests" and "Tests of the library and the
  simulator" above.
- `airflow_automation/` -- everything specific to the Airflow prototype: `airflow_dags/` (the DAG
  design above), `airflow-designs-docs/` (its write-up), `airflow_demo/` (the Airflow
  adapter for the live-test setup below: `airflow_env.sh` and `airflow_demo.sh`) and
  `airflow_containers/` (the image and the container deployments, see "Running Airflow in containers").
  `airflow_dags/` is still imported as the top-level
  package `airflow_dags` -- `airflow_env.sh` and `tests/conftest.py` put `airflow_automation/`
  on the path for that.
- `scenario-player/` -- the engine-agnostic offline simulator, see "The offline simulator" above:
  `sim_env.sh` (scratch environment + fake toolchain bootstrap), `seed.py` and
  `scenario_player.py` (driving/replaying fake runs, with `scenarios/*.yaml`), `faults.py` (the
  shared fault-injection vocabulary), `expectations.py` and `fake_conddb.py` (the `expect:` vocabulary
  and the dummy conditions DB a scenario's result is validated against, see "Validating the
  result" above; it also defines the provenance/ledger formats the fakes write) and `bin/` (the
  fake `cmsRun`/`cmsDriver.py`/... toolchain).
  Nothing in it imports or knows about Airflow specifically, so the same scratch environment and
  scenario files drive both the FSM loops (`live_demo.sh`) and Airflow unchanged -- see `sim_env.sh`'s header
  comment for why that split exists. It's not a Python package (hence the hyphen): `faults.py`,
  `seed.py`, `fake_conddb.py`, `expectations.py` and `scenario_player.py` are top-level modules found
  via `sys.path`
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

## Running a live demo with Airflow

`airflow_automation/airflow_demo/airflow_demo.sh` runs the real Airflow scheduler + dag-processor + API server (in tmux)
against the same fake OMS/EOS/CMSSW toolchain in `scenario-player/bin/` (fake `cmsDriver.py`, `cmsRun`,
`edmFileUtil`, `xrdfs`, `cmsrel`, `cmsenv`, `uploadConditions.py`) and `scenario-player/seed.py` used by the
tests -- bootstrapped via `scenario-player/sim_env.sh`'s `sim_setup`, shared with
`scenario-player/live_demo.sh` (the FSM loops' adapter) and any future engine adapter. Everything runs for real -- real DAG runs, real generated job scripts, real
output files flowing Step 2 -> Step 3 -> Step 4 to a fake condDB upload -- entirely offline.
Airflow has no official Windows support, so this needs a Linux host (WSL works fine on
Windows).

```bash
./airflow_automation/airflow_demo/airflow_demo.sh setup                                # create ./demo-env scratch env
./airflow_automation/airflow_demo/airflow_demo.sh start                                 # scheduler + dag-processor + API server (port: `ui` prints it), tmux
./airflow_automation/airflow_demo/airflow_demo.sh unpause-watch                          # unpause the 4 DAGs (starts the watchers)
./airflow_automation/airflow_demo/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52  # latch a fake live run
./airflow_automation/airflow_demo/airflow_demo.sh ui                                    # print the UI URL
./airflow_automation/airflow_demo/airflow_demo.sh add-ls EcalPedestals 398600 53         # simulate a new lumisection arriving
./airflow_automation/airflow_demo/airflow_demo.sh end-run 398600                         # simulate the run ending
./airflow_automation/airflow_demo/airflow_demo.sh status                                 # list running tmux sessions
./airflow_automation/airflow_demo/airflow_demo.sh stop                                   # tear down all four processes
```

Or let one command do all of that from a cold start, for a whole scenario file (see "Playing a
scenario against Airflow" below):

```bash
./airflow_automation/airflow_demo/airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml --interactive --monitor
```

That last command is also the guided walkthrough, the same as the FSM loops' (`live_demo.sh play
... --interactive --monitor`): each step of the scenario waits for Enter, and every file the
pipeline produces is reported as it appears, so you can watch the run watcher -> run primer ->
file watcher -> process chain actually happening. Next to those files, `--monitor` makes `play`
report Airflow's side of the same activity as it happens -- each Asset event, the DagRun it
triggers and that DagRun's state, any task retry (`airflow_automation/airflow_demo/watch_views.py`)
-- and sum it up at the end, with the watchers' `AssetStateStore` watermarks. Play
`scenario-player/scenarios/total_fault_injection_demo.yaml` the same way to watch crashed jobs retrying.

### Playing a scenario against Airflow

`seed-run`/`add-ls`/`end-run` above are one-shot commands, handy for typing out by hand or
for the paced walkthrough above. `play` plays a whole scenario file instead, mirroring
`./scenario-player/live_demo.sh play` for the FSM loops:

```bash
./airflow_automation/airflow_demo/airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
./airflow_automation/airflow_demo/airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml --interactive --monitor
```

It sets up `$NGT_DEV_HOME` and this checkout's Airflow database if they aren't there, starts
whichever of the four processes aren't running, unpauses the watch DAGs and waits until the
triggerer is running their watchers, then passes every argument straight to
`scenario-player/scenario_player.py` (see "Scripting a timed sequence of runs/lumisections"
above for its flags). `--dry-run` and `--help` skip the bring-up, since they
play nothing; `--monitor` adds Airflow's view of the run to the player's file reports (see
above). The player itself is the same engine-agnostic layer `seed-run`/`add-ls`/`end-run`
are -- it calls `scenario-player/seed.py`'s functions directly, not Airflow -- so the same
scenario files play unchanged against both engines. `play` warns when the scenario's runs were
already played against this instance (the watchers remember the run numbers they've handled, and
ignore them); `reset-scenario` clears that.

TODO: the shipped `expect:` blocks allow a repeated final payload and uploads after it, for the FSM
loops' end-of-run re-upload -- so against Airflow they also let through a DAG re-firing after the
run ended, or a duplicate from two overlapping Step 4 runs.

Run `./airflow_automation/airflow_demo/airflow_demo.sh` with no arguments for the full command list. All demo *data* lives
under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory inside this repo) --
delete it any time to reset the demo environment (the Airflow instance itself -- metadata DB,
`AIRFLOW_HOME` -- is separate/persistent, see "Setting up and running Airflow" above).

### Resetting state

Two independent layers of state can go stale, and clearing one doesn't clear the other:

- **The simulator's own state** (`$NGT_DEV_HOME`: fake OMS runs, EOS files, output data, logs,
  and the `conddb/` upload ledger)
  -- see "Resetting the simulator's state and the give-up timers" above. `airflow_demo.sh setup`
  (which calls the same `sim_setup` internally) always activates the Airflow venv (`$NGT_AIRFLOW_VENV`, default `~/airflow3-ngt-venv`) itself
  first. **Caution**: `./scenario-player/sim_env.sh reset` while Airflow is running is not safe
  -- confirmed live, doing this while Airflow's triggerer had in-flight watcher triggers
  (`ngt_dags_watch.py`'s `AssetWatcher`s) left them stuck polling stale state indefinitely
  (needing a process restart to recover), and silently orphaned each tmux process's own console
  log. If Airflow is running, use `./airflow_automation/airflow_demo/airflow_demo.sh reset-scenario`
  instead (below).

- **Airflow's own state** (DAG-run/task-instance history, in the metadata DB, *not* under
  `$NGT_DEV_HOME`) -- resetting only the simulator leaves stale DagRuns in Airflow's UI
  referencing run numbers no longer on disk, so `airflow_automation/airflow_demo/airflow_demo.sh` has its own commands:
  ```bash
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-watch      # clear ngt_dags_watch.py's 4 DAGs + its static Assets
  ./airflow_automation/airflow_demo/airflow_demo.sh reset-airflow    # full reset: all DAG-run history, variables, connections --
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
  `reset-watch`/`reset-airflow` don't touch `$NGT_DEV_HOME`; `reset-scenario`
  doesn't touch DAG-run history -- reset the ones you need for a fully clean slate between test
  runs.

The Airflow adapter adds one more setting, on top of the simulator's own (`NGT_DEV_HOME`,
`NGT_TEST_MAX_LATCH_TIME_HOURS`, `NGT_TEST_STEP_TIMEOUT_SECONDS` -- see above):

| Variable                          | Default                          | Meaning                          |
|-------------------------------------|-----------------------------------|-----------------------------------|
| `NGT_RUN_END_GRACE_SECONDS` | `20` (production default: `1800`) | how long a file watcher keeps waiting after a run *ends* before giving up -- see below |

e.g. `NGT_DEV_HOME=/tmp/ngt-demo ./airflow_automation/airflow_demo/airflow_demo.sh setup`. `NGT_RUN_END_GRACE_SECONDS` is read once per DAG
parse (it's a module-level default in `_process_tasks.py`, not a calibrationYAML key), so
the dag-processor needs to re-parse after changing it.

**Why `NGT_RUN_END_GRACE_SECONDS` exists**: the simulator's two give-up timers are
measured from a run's *start*; a run that runs only part of that budget before *ending* would
otherwise still leave the file watcher polling for whatever's left
of it -- confirmed live. Rather than changing `ngt_calibration_loop`'s shared decision logic
(`step2.py`, whose decision logic stays unmodified), this one lives
entirely in `airflow_automation/airflow_dags/triggers.py`'s `LumisectionFileWatcherTrigger`: it
queries OMS directly for the run's
actual end time and gives up once that's `NGT_RUN_END_GRACE_SECONDS` in the past,
independent of `check_ls_for_processing`'s own (unmodified) run-start-relative timeout. See
`watch_asset_scheduling_design.md` for the full writeup.

## Airflow DAG design

The DAG set is built from the same, completely unmodified `ngt_calibration_loop` processing
logic and the same fake OMS/EOS/CMSSW live-test setup the pytest suite uses, so it can be
exercised end to end against the scripted scenarios in `scenario-player/scenarios/*.yaml`
(played by `scenario-player/scenario_player.py`). Full writeup:
[`watch_asset_scheduling_design.md`](airflow_automation/airflow-designs-docs/watch_asset_scheduling_design.md)
-- DAG-by-DAG, sequence diagrams, batching semantics, an introduction to the Airflow 3 Asset
machinery it is built on, and why per-run assets are not an option.

**`airflow_automation/airflow_dags/ngt_dags_watch.py`**: 4 DAGs, **zero cron** -- every asset
static, driven by `AssetWatcher`s.

- `Asset("ngt://runs")` with a `RunWatcherTrigger` -- an event-driven `BaseEventTrigger` that
  runs continuously in the `triggerer` (while `ngt_watch_run_primer` is unpaused), polls OMS
  calibration-agnostically, and yields one event per new run.
- `ngt_watch_run_primer` (`schedule=[Asset("ngt://runs")]`) -- calls `step2.find_new_run` per
  calibration to create the working dirs the file watchers gate on. (An `AssetWatcher` can't be
  *spawned* by an event; the file watchers are always running, just idle until a run dir exists.)
- `Asset("ngt://files/<cal>")` x3 with a `LumisectionFileWatcherTrigger` -- watches that
  calibration's active run dir and yields **one event per file** (reusing
  `step2.check_ls_for_processing` unchanged as the "is there something yet" check); writes
  `runEnd.log` on run-end. Dedup watermarks (seen runs / emitted files) live in each Asset's
  `AssetStateStore`.
- `ngt_watch_process_<cal>` (`schedule=[Asset("ngt://files/<cal>")]`) x3 -- one DAG **per
  calibration**, so each calibration's runs/logs/Grid view are separately browsable and pausable
  in the Airflow UI. Runs the one file from the triggering Asset event through
  `step2_express >> step3_alca >> step4_harvest >> step4_upload`. Step 2 and Step 3 each act on
  only that one file; Step 4 is the deliberate exception -- it harvests/uploads from *every*
  ALCARECO file accumulated for the run so far, every time (this is `step4.py`'s own pre-existing
  "reprocess everything" behavior, reused unchanged -- more statistics genuinely improve the
  payload). The execution flow is still one DagRun per file; Step 4 just looks at more than its
  own triggering file's data.

This needs Airflow's `triggerer` process running, since it hosts all four watchers
(`airflow_automation/airflow_demo/airflow_demo.sh start` launches it alongside the scheduler,
dag-processor and API server). Pausing the `ngt_watch_*` DAGs stops the watchers.

The trade-off, stated plainly: a watcher only runs while its consuming DAG is unpaused, and the
triggerer restarting a watcher after each yield is also a window in which it could re-yield a
file -- which is why the `emitted_run<N>` watermark is persisted *before* the yield and
`step2_express` additionally skips a file already in `allLSProcessed.log`. A calibration whose
files never arrive for an OMS-visible run (e.g. only SiStripBad gets seeded for a given run
number in a test scenario) keeps its file watcher polling until the run-end grace period (or, if
the run never ends, `step2.py`'s own give-up timer) expires -- inherited from
`oms.find_new_run`/`check_ls_for_processing` unchanged.

Quick start (`play` sets up, starts and unpauses whatever isn't yet -- unpausing the watch DAGs is
also what starts the 4 AssetWatchers in the triggerer; see "Playing a scenario against Airflow"
above):

```bash
./airflow_automation/airflow_demo/airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
# watch Asset ngt://runs -> ngt_watch_run_primer -> Asset ngt://files/ecalpedestals
#       -> ngt_watch_process_ecalpedestals   (all 4 assets show in the UI Assets view)
./airflow_automation/airflow_demo/airflow_demo.sh pause-watch              # stops the watchers
```

For a paced walkthrough instead, add `--interactive --monitor` (and play
`scenario-player/scenarios/total_fault_injection_demo.yaml` to watch crashed jobs retrying).

`tests/airflow-ngt/test_dags_watch.py` covers the design (see "Tests of the library and the simulator" above).

## Public Presentations

- **Musich, M.** et al. (2025) *Task 3.4: Optimal Calibrations for the CMS High-Level Trigger*. Next Generation Triggers 2nd Technical Workshop, CERN, 21 November 2025. [DOI](https://doi.org/10.17181/gzvw9-t3379).

- **Zarucki, M.** (2026). *Demonstrating the Processing Chain for the Next Generation Triggers in the CMS Experiment*. 28th Conference on Computing in High Energy and Nuclear Physics (CHEP 2026), CMS, CERN. [DOI](https://doi.org/10.17181/txk7r-fsd29).

- **Prendi, J.** (2026). *Conceptual Design and Operation of the Calibration Loop for the Next Generation Triggers in the CMS Experiment*. 28th Conference on Computing in High Energy and Nuclear Physics (CHEP 2026), CERN, 28 May 2026. [DOI](https://doi.org/10.17181/rv6ad-zpy87).
