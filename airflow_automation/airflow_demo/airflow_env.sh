#!/bin/bash
# Sourced by airflow_demo.sh (and usable standalone) to configure a local
# Airflow 3.x instance pointed at this repo's airflow_automation/airflow_dags/, backed by the
# local `airflow_ngt` Postgres role/database (LocalExecutor needs row-level
# locking, which SQLite doesn't support -- see README.md's Airflow setup
# section for how that role/database was created).
#
# This is the Airflow-*specific* half of the live-test setup -- infrastructure
# env vars only (executor, DB, DAGs folder, auth). The engine-agnostic half
# (scratch env location, fake toolchain bootstrap) lives in scenario-player/sim_env.sh,
# sourced below; demo-specific wiring (fake OMS/EOS/CMSSW toolchain env vars)
# lives in airflow_demo.sh itself, same split scenario-player/sim_env.sh documents.
#
# Usage: source airflow_automation/airflow_demo/airflow_env.sh

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scenario-player" && pwd)/sim_env.sh"

# Named distinctly from a pre-existing Airflow 2.x install rather than
# reusing its AIRFLOW_HOME/database in place -- an in-place major-version
# upgrade is real work Airflow supports, but not something worth doing for a
# disposable dev/demo instance; a fresh, separately-named one sidesteps a
# schema mismatch without touching (or needing) whatever was there before.
export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow3-ngt}"
export AIRFLOW__CORE__DAGS_FOLDER="$REPO_DIR/airflow_automation/airflow_dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://airflow_ngt:airflow_ngt@localhost/airflow3_ngt"

# Airflow 3 splits DAG parsing out of the scheduler into its own mandatory
# `airflow dag-processor` process (see airflow_demo.sh's cmd_start) and
# renamed these two settings out of [scheduler] accordingly -- confirmed via
# `airflow config lint` against this repo's old (Airflow 2) settings.
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=15
export AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL=10

# Likewise `[webserver] expose_config` moved to `[api] expose_config` now that
# the API server (airflow_demo.sh's cmd_start) is its own component rather
# than part of a combined webserver process.
export AIRFLOW__API__EXPOSE_CONFIG=True
export NGT_AIRFLOW_PORT="${NGT_AIRFLOW_PORT:-8090}"

# Task code can no longer reach Airflow's metadata DB directly (Airflow 3
# isolates task execution from the metadata database) -- it talks to the API
# server over HTTP instead, so the API server's own worker-facing "execution
# API" URL has to be told which host:port it's actually listening on
# (cmd_start passes $NGT_AIRFLOW_PORT to `airflow api-server -p`; this must
# match).
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://localhost:$NGT_AIRFLOW_PORT/execution/"

# Simple Auth Manager (Airflow 3's dev/test-oriented default, replacing FAB)
# treats every request as admin with no username/password needed -- exactly
# what an offline, single-user dev/demo instance like this one wants; see
# README.md's Airflow setup section for why this is an appropriate fit here
# specifically.
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True

# $REPO_DIR for ngt_calibration_loop; airflow_automation/ so the DAG files
# can `import airflow_dags` (the package name, unchanged by the directory move --
# it's also baked into the triggers' serialized classpaths in Airflow's DB).
export PYTHONPATH="$REPO_DIR:$REPO_DIR/airflow_automation:${PYTHONPATH:-}"
