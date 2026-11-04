# The Airflow prototype as one image: Airflow 3.3.1 plus this repo's DAGs, library and fake
# OMS/EOS/CMSSW toolchain, at the same relative paths as in a checkout. Every way of running it
# (onepod/, compose/) uses this image, differing only in the command and environment each
# container gets. Build it with the repo root as context; see README.md next to this file.
#
# Slim: the same five providers as the venv README.md's setup section creates, and well under half
# the size of the full image (which ships ~100 providers nothing here imports). It has no database
# drivers, so psycopg2 and asyncpg are added, at the versions Airflow's own constraints pin.
ARG AIRFLOW_BASE_IMAGE=apache/airflow:slim-3.3.1-python3.12
FROM ${AIRFLOW_BASE_IMAGE}

RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" psycopg2-binary asyncpg \
      --constraint "${HOME}/constraints.txt"

COPY --chown=airflow:root ngt_calibration_loop/ /opt/ngt/ngt_calibration_loop/
COPY --chown=airflow:root airflow_automation/ /opt/ngt/airflow_automation/
COPY --chown=airflow:root scenario-player/ /opt/ngt/scenario-player/
COPY --chown=airflow:root calibrationYAML/ /opt/ngt/calibrationYAML/
COPY --chown=airflow:root tests/stubs/ /opt/ngt/tests/stubs/

# A plain install rather than the editable one sim_setup makes: the code never changes in an image,
# and it has to be importable where $NGT_DEV_HOME/env.sh (and so PYTHONPATH) isn't, in a container
# that doesn't mount the scratch volume. sim_setup skips its own (NGT_SIM_STUB_INSTALLED).
RUN pip install --no-cache-dir --no-deps /opt/ngt/tests/stubs

USER root
# A cluster may run a container as an arbitrary UID with GID 0 (OpenShift does), so everything the
# image's processes write to has to be writable by group 0 (the setgid keeps it that way for what
# they create), and everything they run must not depend on being UID 50000. Same reasoning as
# Airflow's own image:
# https://airflow.apache.org/docs/docker-stack/entrypoint.html#allowing-arbitrary-user-to-run-the-container
RUN mkdir -p /ngt-data && chown airflow:root /ngt-data && chmod 2775 /ngt-data \
    && chmod -R a+rX,go-w /opt/ngt
# The airflow user, by number as in Airflow's own image: a cluster can't check runAsNonRoot on a name.
USER 50000

# NGT_DEV_HOME is a subdirectory, not the mount point itself: sim_reset deletes it.
ENV NGT_AIRFLOW_IN_CONTAINER=1 \
    NGT_SIM_STUB_INSTALLED=1 \
    NGT_DEV_HOME=/ngt-data/demo-env \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/ngt/airflow_automation/airflow_dags \
    PYTHONPATH=/opt/ngt:/opt/ngt/airflow_automation \
    PATH=/opt/ngt/airflow_automation/airflow_demo:${PATH}

WORKDIR /opt/ngt

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/opt/ngt/airflow_automation/airflow_containers/container_entrypoint.sh"]
