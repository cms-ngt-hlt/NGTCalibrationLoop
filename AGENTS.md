# NGTCalibrationLoop — agent instructions

Calibration-leg pipeline for the CMS NGT demonstrator: watches live LHC runs, reprocesses
raw data through three steps, and uploads the resulting calibration payload to CMS's
conditions database. Mid-migration from a hand-rolled FSM/`while True` polling design to
Apache Airflow; two alternate Airflow DAG designs currently coexist under `airflow_automation/airflow_dags/`
for side-by-side comparison (see README.md's "Airflow DAG designs" section). Read
README.md first for architecture and design rationale — this file only covers what
README.md doesn't: conventions, not architecture.

This file is shared: Codex and other agents read it directly, Claude Code loads it via
`CLAUDE.md`'s `@AGENTS.md` import. Keep it tool-agnostic and machine-agnostic — anything
specific to one person's OS/venv/process setup belongs in a local, gitignored override
(`CLAUDE.local.md` for Claude Code), not here.

## Repo layout

- `ngt_calibration_loop/` — processing logic (OMS/EOS queries, cmsDriver prep, batching),
  as plain functions with no Airflow dependency. `step2.py`/`step3.py`/`step4.py` per step;
  `config.py`/`oms.py`/`eos.py`/`shell.py` shared helpers; `original_fsm_only/` the few
  functions only the original FSM used (no Airflow design calls them; tests in
  `tests/test_original_fsm_only.py`).
- `airflow_automation/` — everything specific to the Airflow prototypes:
  - `airflow_dags/` — the two DAG designs (see README.md) plus `airflow_api.py`, the shared
    wrapper around Airflow's REST API used for cross-DAG coordination. Still imported as the
    top-level package `airflow_dags` (`airflow_automation/` is what goes on `PYTHONPATH`).
  - `airflow-designs-docs/` — write-ups of a DAG design that's actually built and living in
    `airflow_dags/` (one doc per design: sequence diagrams, batching semantics, trade-offs).
  - `airflow_demo/` — the Airflow adapter for the live-test setup: `airflow_env.sh`,
    `airflow_demo.sh`, `*_interactive_demo.sh`.
- `scenario-player/` — the engine-agnostic offline simulator, with no Airflow dependency:
  `sim_env.sh`, `seed.py`, `scenario_player.py` (+ `scenarios/*.yaml`), `faults.py` (the shared
  fault-injection vocabulary) and `bin/`, the fake OMS/EOS/CMSSW toolchain. Not a Python
  package: `faults`/`seed`/`scenario_player` are top-level modules found via `sys.path`
  (see `tests/conftest.py`, `sim_env.sh`'s `PYTHONPATH`).
- `calibrationYAML/` — per-calibration config.
- `tests/` — pytest suite; `tests/stubs` is the fake `omsapi` package (also `pip install -e`'d
  by the live demo), `tests/support` has fake-subprocess/EOS helpers used across the step2/3/4
  tests, `tests/airflow_designs/` holds the DAG tests (don't name a test dir `airflow` — with
  `tests/` on `sys.path` it shadows the real package and breaks `importorskip("airflow")`).
- `research-docs/` — shareable background reading and pre-implementation analysis (not
  documentation of something already built); `research-docs/design-analysis/` is
  specifically for analysis weighing options ahead of a decision, as opposed to general
  reference material. `research-docs/ignored-research/` (gitignored) holds the same kind
  of content before it's ready to share.
- `ignored-prototypes/` — evaluations of workflow engines this project didn't end up
  using. Gitignored wholesale and its own nested git repo with its own history — don't
  treat its contents as tracked by, or expect them to be referenced from, this repo's
  own history.

## Setup & commands

```bash
pip install -e .                    # ngt_calibration_loop, used by tests and the DAGs
pip install -r requirements-dev.txt
pytest                              # Airflow-dependent tests skip cleanly when
                                     # apache-airflow isn't installed
```

CI (`.github/workflows/pylint.yml`) runs `pylint`, `isort --check-only` and `flake8` over
`ngt_calibration_loop/` and `airflow_automation/` (the library and the DAGs -- not `tests/` or
`scenario-player/`); settings are in `pyproject.toml` and `.flake8`, so `pip install pylint isort
flake8` and running each on those two directories reproduces CI.

For running Airflow itself, the live tmux-based demo, or `scenario-player/scenario_player.py`
scenarios, see README.md's "Setting up and running Airflow" / "Running a live demo"
sections, and your local override file for this machine's exact setup. Don't
re-document those commands here — one of the two copies will drift.

## Committing

- Commit at meaningful milestones. Don't end a turn that changed files meant to be kept
  without committing them.
- Match the log style: a short, imperative subject that starts with the part of the repo it
  affects -- `Airflow-general:`, `Airflow-per-file-prototype:`, `Airflow-asset-watch-prototype:`,
  `Tests:`, `Scenario-tests:`, `Core:`, `Fix:`, `Docs:`, `Research:`, `Repo:` -- optionally with a
  trailing parenthetical or `--` clause explaining why. Not Conventional Commits. Keep
  engine-agnostic changes (tests, mocks, scenario player, core library) in separate commits
  from Airflow-specific ones, and put each prototype's code, tests, demo wiring and design
  doc behind that prototype's own prefix, so a prototype can be reviewed or dropped as a unit.
- A session's commits can be reorganized afterward into fewer, more coherent ones if they
  ended up too granular — but only on unpushed work, and only after creating a backup
  branch first: `git branch backup/<branch>-<YYYYMMDD>[-<short-note>]` (see this repo's
  existing `backup/*` branches for the convention already in use).

## Where does a new .md doc go?

- Documents a DAG design that's actually implemented → `airflow_automation/airflow-designs-docs/`.
- Shareable background reading or analysis of options ahead of a decision →
  `research-docs/` (`research-docs/design-analysis/` if it's weighing a specific pending
  decision rather than general reference).
- Same, but not yet shareable → `research-docs/ignored-research/`.
- Tied to one of the abandoned/experimental engine prototypes → inside
  `ignored-prototypes/` itself.
- Just explains how a specific module/function works → a docstring/comment in the code,
  not a new top-level doc.
- **Unclear which of these fits? Ask before placing it.**
