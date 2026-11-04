#!/usr/bin/env bash
# What the one-pod deployment's Airflow container runs (ngt-airflow-onepod.yaml): the database and
# the scratch data set up, the login to be an Admin, then every Airflow component. All state is on
# the pod's emptyDirs, so it survives this container being restarted, and a new pod starts clean.
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

# Generated once per pod, and only readable from inside it: `kubectl exec` is what hands it out.
admin_password_file="$AIRFLOW_HOME/admin_password"
if [ ! -f "$admin_password_file" ]; then
  admin_password="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  airflow users create --username admin --role Admin --firstname NGT --lastname Admin \
    --email admin@example.invalid --password "$admin_password"
  (umask 077 && echo "$admin_password" > "$admin_password_file")
fi

# For the Home page's banner: airflow_local_settings.py, next to this script.
export PYTHONPATH="$PYTHONPATH:$(dirname "${BASH_SOURCE[0]}")"

# Not `airflow standalone`, which forces Simple Auth Manager whatever is configured. As soon as one
# component dies the container goes with it, for the kubelet to restart. Not a pipe into sed, which
# wait would see out too: a dead scheduler's workers go on holding the pipe open.
for component in scheduler dag-processor triggerer api-server; do
  airflow "$component" > >(sed -u "s/^/$component | /") 2>&1 &
done
wait -n
