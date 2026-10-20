#!/bin/bash
# Sourced by airflow_demo.sh (and usable standalone) to configure a local
# Airflow 3.x instance pointed at this repo's airflow_automation/airflow_dags/, backed by a
# per-checkout database on the local Postgres (LocalExecutor needs row-level
# locking, which SQLite doesn't support -- see README.md's Airflow setup
# section for how the `airflow_ngt` role was created).
#
# This is the Airflow-*specific* half of the live-test setup -- infrastructure
# env vars only (executor, DB, DAGs folder, auth). The engine-agnostic half
# (scratch env location, fake toolchain bootstrap) lives in scenario-player/sim_env.sh,
# sourced below; the fake OMS/EOS/CMSSW toolchain env vars are in the
# $NGT_DEV_HOME/env.sh its sim_setup writes, which airflow_demo.sh's tmux sessions
# source on top of the values resolved here.
#
# Isolation between checkouts: every value below that could collide between two
# checkouts of this repo on one machine (Airflow home, metadata DB, API-server port,
# log-server ports, tmux session names) defaults to something derived from
# $NGT_INSTANCE (sim_env.sh: this checkout's basename + a hash of its path) or
# $NGT_INSTANCE_HASH, so two checkouts can run side by side with no configuration.
# Every one of them is overridable -- from the environment, or persistently from the
# gitignored local_airflow_env.sh next to this file (which beats the environment); see
# local_airflow_env.sh.example. Either way the value reaches all four Airflow processes:
# airflow_demo.sh hands its tmux sessions the settings exactly as resolved here instead of
# letting them inherit the tmux server's environment (see its write_session_env).
#
# Usage: source airflow_automation/airflow_demo/airflow_env.sh

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scenario-player" && pwd)/sim_env.sh"

# Optional, gitignored per-checkout overrides. Sourced first, so plain `VAR=value` lines in
# it win over the `${VAR:-default}` defaults below; it sees $REPO_DIR, $NGT_DEV_HOME,
# $NGT_INSTANCE and $NGT_INSTANCE_HASH (sim_env.sh has already run, including its own
# local_sim_env.sh). Skipped under NGT_SKIP_LOCAL_ENV=1, like that one.
# shellcheck disable=SC1091
if [ -z "${NGT_SKIP_LOCAL_ENV:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/local_airflow_env.sh" ]; then
  source "$(dirname "${BASH_SOURCE[0]}")/local_airflow_env.sh"
fi

# The venv airflow_demo.sh/watch_interactive_demo.sh activate (see README.md's setup
# section). Shared between checkouts by default -- it holds no per-checkout state, apart
# from the editable installs `sim_setup` makes, which any checkout can safely repeat.
export NGT_AIRFLOW_VENV="${NGT_AIRFLOW_VENV:-$HOME/airflow3-ngt-venv}"

# Named distinctly from a pre-existing Airflow 2.x install rather than
# reusing its AIRFLOW_HOME/database in place -- an in-place major-version
# upgrade is real work Airflow supports, but not something worth doing for a
# disposable dev/demo instance; a fresh, separately-named one sidesteps a
# schema mismatch without touching (or needing) whatever was there before.
# Per checkout, so two checkouts never share an airflow.cfg (and with it the JWT
# secrets the API server and its workers must agree on) or task logs.
export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow3-ngt-$NGT_INSTANCE}"

# Metadata DB: a per-checkout database owned by the shared `airflow_ngt` role.
# `airflow_demo.sh setup` creates it (that role has CREATEDB) and migrates it, so nothing
# needs doing by hand. Without this, two checkouts would share one DB and fight over its
# DAGs, and `reset-airflow` in either would wipe the other's state.
export NGT_AIRFLOW_DB_HOST="${NGT_AIRFLOW_DB_HOST:-localhost}"
export NGT_AIRFLOW_DB_PORT="${NGT_AIRFLOW_DB_PORT:-5432}"
export NGT_AIRFLOW_DB_USER="${NGT_AIRFLOW_DB_USER:-airflow_ngt}"
export NGT_AIRFLOW_DB_PASSWORD="${NGT_AIRFLOW_DB_PASSWORD:-airflow_ngt}"
export NGT_AIRFLOW_DB_NAME="${NGT_AIRFLOW_DB_NAME:-airflow3_ngt_$NGT_INSTANCE}"
export NGT_AIRFLOW_DB_CONN="postgresql+psycopg2://$NGT_AIRFLOW_DB_USER:$NGT_AIRFLOW_DB_PASSWORD@$NGT_AIRFLOW_DB_HOST:$NGT_AIRFLOW_DB_PORT/$NGT_AIRFLOW_DB_NAME"
# Set AIRFLOW__DATABASE__SQL_ALCHEMY_CONN itself to point at any other database (e.g. a
# remote one); `setup` then leaves creating it to you.
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="${AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-$NGT_AIRFLOW_DB_CONN}"

export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW__CORE__DAGS_FOLDER:-$REPO_DIR/airflow_automation/airflow_dags}"
export AIRFLOW__CORE__LOAD_EXAMPLES="${AIRFLOW__CORE__LOAD_EXAMPLES:-False}"
export AIRFLOW__CORE__EXECUTOR="${AIRFLOW__CORE__EXECUTOR:-LocalExecutor}"

# Airflow 3 splits DAG parsing out of the scheduler into its own mandatory
# `airflow dag-processor` process (see airflow_demo.sh's cmd_start) and
# renamed these two settings out of [scheduler] accordingly -- confirmed via
# `airflow config lint` against this repo's old (Airflow 2) settings.
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL="${AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL:-15}"
export AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL="${AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL:-10}"

# Likewise `[webserver] expose_config` moved to `[api] expose_config` now that
# the API server (airflow_demo.sh's cmd_start) is its own component rather
# than part of a combined webserver process.
export AIRFLOW__API__EXPOSE_CONFIG="${AIRFLOW__API__EXPOSE_CONFIG:-True}"

# Ports. The API server/UI gets 8100-8899, picked from the checkout's path hash so two
# checkouts don't land on the same one; the scheduler's and triggerer's task-log servers
# (Airflow's own defaults, 8793/8794, are the same for every instance) are offset from it.
# A collision on the hash is possible but unlikely -- override NGT_AIRFLOW_PORT if it happens.
export NGT_AIRFLOW_PORT="${NGT_AIRFLOW_PORT:-$((8100 + NGT_INSTANCE_HASH % 800))}"
export AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT="${AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT:-$((NGT_AIRFLOW_PORT + 1000))}"
export AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT="${AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT:-$((NGT_AIRFLOW_PORT + 2000))}"

# Task code can no longer reach Airflow's metadata DB directly (Airflow 3
# isolates task execution from the metadata database) -- it talks to the API
# server over HTTP instead, so the API server's own worker-facing "execution
# API" URL has to be told which host:port it's actually listening on
# (cmd_start passes $NGT_AIRFLOW_PORT to `airflow api-server -p`; this must
# match).
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="${AIRFLOW__CORE__EXECUTION_API_SERVER_URL:-http://localhost:$NGT_AIRFLOW_PORT/execution/}"

# Simple Auth Manager (Airflow 3's dev/test-oriented default, replacing FAB)
# treats every request as admin with no username/password needed -- exactly
# what an offline, single-user dev/demo instance like this one wants; see
# README.md's Airflow setup section for why this is an appropriate fit here
# specifically.
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS="${AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS:-True}"

# Prefix of the four tmux sessions airflow_demo.sh manages (<prefix>Scheduler, DagProcessor,
# ApiServer, Triggerer). tmux session names are global to one tmux server, so without a
# per-checkout prefix a second checkout's `start` would find the first one's sessions
# already running and silently do nothing.
export NGT_AIRFLOW_TMUX_PREFIX="${NGT_AIRFLOW_TMUX_PREFIX:-NGTAirflow_${NGT_INSTANCE}_}"

# $REPO_DIR for ngt_calibration_loop; airflow_automation/ so the DAG files
# can `import airflow_dags` (the package name, unchanged by the directory move --
# it's also baked into the triggers' serialized classpaths in Airflow's DB).
export PYTHONPATH="$REPO_DIR:$REPO_DIR/airflow_automation:${PYTHONPATH:-}"
