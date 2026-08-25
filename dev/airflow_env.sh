#!/bin/bash
# Sourced by dev/airflow_demo.sh (and usable standalone) to configure a local
# Airflow instance pointed at this repo's airflow_dags/, backed by the local
# `airflow_ngt` Postgres role/database (LocalExecutor needs row-level locking,
# which SQLite doesn't support -- see README.md's Airflow setup section for how
# that role/database was created).
#
# This file only sets *infrastructure* env vars (executor, DB, dags folder) --
# demo-specific wiring (fake OMS/EOS/CMSSW toolchain, NGT_* overrides) lives in
# dev/airflow_demo.sh itself, same split as dev/live_demo.sh already uses.
#
# Usage: source dev/airflow_env.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow-ngt}"
export AIRFLOW__CORE__DAGS_FOLDER="$REPO_DIR/airflow_dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__EXECUTOR=LocalExecutor
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://airflow_ngt:airflow_ngt@localhost/airflow_ngt"
export AIRFLOW__WEBSERVER__EXPOSE_CONFIG=True
export AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL=15
export AIRFLOW__SCHEDULER__MIN_FILE_PROCESS_INTERVAL=10

export PYTHONPATH="$REPO_DIR:${PYTHONPATH:-}"
