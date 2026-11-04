#!/usr/bin/env bash
# In front of Airflow's own /entrypoint (arbitrary-UID handling, waiting for the database, the
# `airflow <component>` dispatch): the fake toolchain's environment, the same $NGT_DEV_HOME/env.sh
# airflow_demo.sh's tmux sessions source. Only where sim_setup has written it -- not yet in the
# container that runs it, and never in one that doesn't mount the scratch volume.
# shellcheck disable=SC1091
[ -f "$NGT_DEV_HOME/env.sh" ] && source "$NGT_DEV_HOME/env.sh"
exec /entrypoint "$@"
