#!/usr/bin/env bash
# What the multi-pod deployment runs before Airflow's components, as an init container of the scheduler (../values.yaml):
# the database migrated, the fake data set up the first time, and the admin login. The other components wait for the
# migrations, and the triggerer and the tools pod for the fake data as well. Everything it makes is on persistent
# volumes, so a restart only migrates again.
set -euo pipefail

# Postgres is a pod of its own, which may still be starting.
until airflow db check > /dev/null 2>&1; do
  echo "Waiting for the database..."
  sleep 2
done

if [ -f "$NGT_DEV_HOME/env.sh" ]; then
  airflow db migrate
else
  airflow_demo.sh setup
fi

# Once, with the Secret's password, which the database then keeps: changing it later is done in the UI.
if ! airflow users list --output json \
    | python3 -c 'import json, sys; sys.exit(not any(u["username"] == "admin" for u in json.load(sys.stdin)))'; then
  airflow users create --username admin --role Admin --firstname NGT --lastname Admin \
    --email admin@example.invalid --password "$NGT_ADMIN_PASSWORD"
fi

# Task and DAG-parsing logs only pile up on their volume otherwise.
find "$AIRFLOW_HOME/logs" -type f -mtime +14 -delete
