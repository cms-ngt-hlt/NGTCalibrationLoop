#!/usr/bin/env bash
# What the one-pod deployment's Airflow container runs (ngt-airflow-onepod.yaml): the database and
# the scratch data set up, then every Airflow component under one `airflow standalone`. All state is
# on the pod's emptyDirs, so it survives this container being restarted, and a new pod starts clean.
set -euo pipefail

mkdir -p "$AIRFLOW_HOME"

# Postgres is a sibling container, started at the same time. (The image's entrypoint only waits for
# the database in front of the individual components' commands.)
until airflow db check > /dev/null 2>&1; do
  echo "Waiting for the database..."
  sleep 2
done

airflow_demo.sh setup
# shellcheck disable=SC1091
source "$NGT_DEV_HOME/env.sh"

exec airflow standalone
