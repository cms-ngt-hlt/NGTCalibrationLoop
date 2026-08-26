# The NGT Demonstrator Calibration Leg

## Overview
This directory implements the calibration leg workflow of our NGT demonstrator. It is designed to run the calibration loop on either of our calibration nodes (`ngtcalfu-c2b05-43-01.cms` and `ngtcalfu-c2b05-44-01.cms`) for the NGT demonstrator, through monitoring ongoing collisions at CMS and rederiving calibrations (for now only EcalPedestals and SiStrip Bad Components) up to the upload to the conditions database of CMS. Each calibration loop is implemented as a finite state machine (FSM) that monitors input data, processes it, and produces the output for the next step:

- **Step 2**: Monitors OMS for new runs, processes raw files files from EOS, produces (RE)RECO files
- **Step 3**: Monitors for step 2 output files, merges them, produces ALCARECO root files
- **Step 4**: Monitors for step 3 output files, performs harvesting, produces and uploads final payload to condDB to be consumed by the NGT Global Tag

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

For watching the actual FSM processes run -- not just pytest -- `dev/live_demo.sh`
launches real `NGTLoopStep2/3/4.py` processes (one per tmux session) against a fake
OMS/EOS/CMSSW toolchain in `dev/bin/` (fake `cmsDriver.py`, `cmsRun`, `edmFileUtil`,
`xrdfs`, `cmsrel`, `cmsenv`, `uploadConditions.py`). Everything runs for real --
real state transitions, real generated job scripts, real output files flowing from
Step 2 through Step 3 to a fake Step 4 upload -- entirely offline.

```bash
./dev/live_demo.sh setup                                  # create ./demo-env scratch env
./dev/live_demo.sh start-all EcalPedestals                 # launch all 3 steps in tmux
./dev/live_demo.sh seed-run EcalPedestals 398600 --ls 51 52  # latch a fake live run
tmux attach -t NGTDemo2_EcalPedestals                       # watch it react (Ctrl-b d to detach)
./dev/live_demo.sh add-ls EcalPedestals 398600 53           # simulate a new lumisection arriving
./dev/live_demo.sh end-run 398600                           # simulate the run ending
./dev/live_demo.sh status                                   # list running sessions
./dev/live_demo.sh stop-all EcalPedestals                   # tear down
```

For a guided, narrated walkthrough of all of the above -- runs `setup`, launches all
three steps, seeds a couple of runs (a few lumisections each, seeded incrementally),
and after every command shows you the relevant output directories/files so you can
see each state machine actually doing something, pausing between steps -- use:
```bash
./dev/interactive_demo.sh                                    # 2 runs, 2 LS each, EcalPedestals
./dev/interactive_demo.sh --calibration SiStripBad --runs 3 --ls-per-run 4
./dev/interactive_demo.sh --yes                               # don't pause between steps
```

Run `./dev/live_demo.sh` with no arguments for the full command list. All state
lives under `$NGT_DEV_HOME` (default `./demo-env`, a gitignored directory inside
this repo) -- delete it any time to reset the demo environment.

Two settings are overridable via environment variables:

| Variable                 | Default                              | Meaning                          |
|---------------------------|--------------------------------------|-----------------------------------|
| `NGT_DEV_HOME`             | `./demo-env`                         | scratch state directory           |
| `NGT_LOOP_SLEEP_SECONDS`   | `5` (production default: `60`)       | Step 3/4 poll interval, seconds   |

e.g. `NGT_DEV_HOME=/tmp/ngt-demo ./dev/live_demo.sh setup`. `NGT_DEV_HOME` is read
at `setup` time and baked into `$NGT_DEV_HOME/ngtParameters.jsn`, so re-run `setup`
after changing it. `NGT_LOOP_SLEEP_SECONDS` is read fresh each time a loop is
started (`start2` / `start3` / `start4` / `start-all`).

## Nota Bene
There are quite a lot of issues remaining, still. The version we are at right now is the "functioning" version that was used for the demonstrator in the 2025 data taking. However, for 2026 data-taking, we plan to improve and have worked on all the issues. 
