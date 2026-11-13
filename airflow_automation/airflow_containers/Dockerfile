# The Airflow prototype as one image: Airflow 3.3.1 plus this repo's DAGs, library and fake
# OMS/EOS/CMSSW toolchain, at the same relative paths as in a checkout. Every way of running it
# (onepod/, compose/) uses this image, differing only in the command and environment each
# container gets. Build it with the repo root as context; see README.md next to this file.
#
# Slim: the same five providers as the venv README.md's setup section creates, and well under half
# the size of the full image (which ships ~100 providers nothing here imports). It has no database
# drivers and no FAB, so psycopg2, asyncpg and the FAB provider (what onepod/ logs in with) are
# added, at the versions Airflow's own constraints pin. So is the Kubernetes provider: on a cluster,
# the process DAGs can run each step in a pod of another image, a CMSSW one (KubernetesPodOperator).
ARG AIRFLOW_BASE_IMAGE=apache/airflow:slim-3.3.1-python3.12
FROM ${AIRFLOW_BASE_IMAGE}

RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" apache-airflow-providers-fab \
      apache-airflow-providers-cncf-kubernetes psycopg2-binary asyncpg --constraint "${HOME}/constraints.txt"

# Security fixes released since Airflow's constraints were pinned, which Harbor's scan (Trivy) found on
# 2026-11-09: each at the first version that fixes it, as long as what requires it allows that, which
# pip check makes sure of. uv and prek (a pre-commit runner, with a uv of its own) only build and lint;
# nothing in a container runs them.
RUN pip install --no-cache-dir PyJWT==2.15.0 certifi==2026.10.18 dnspython==2.9.0 Mako==1.4.2 \
      msgpack==1.2.3 sqlparse==0.6.0 urllib3==2.8.0 Werkzeug==3.1.9 \
    && pip uninstall --yes uv \
    && rm -rf "${HOME}/.local/share/uv" "${HOME}/.local/bin/prek" \
    && pip check

COPY --chown=airflow:root ngt_calibration_loop/ /opt/ngt/ngt_calibration_loop/
COPY --chown=airflow:root airflow_automation/ /opt/ngt/airflow_automation/
COPY --chown=airflow:root scenario-player/ /opt/ngt/scenario-player/
COPY --chown=airflow:root calibrationYAML/ /opt/ngt/calibrationYAML/
COPY --chown=airflow:root tests/stubs/ /opt/ngt/tests/stubs/

# A plain install rather than the editable one sim_setup makes: the code never changes in an image,
# and it has to be importable where $NGT_DEV_HOME/env.sh (and so PYTHONPATH) isn't, in a container
# that doesn't mount the scratch volume. sim_setup skips its own (NGT_SIM_STUB_INSTALLED). It imports
# scenario-player's faults.py, which env.sh puts on PYTHONPATH: the image does so too, below.
RUN pip install --no-cache-dir --no-deps /opt/ngt/tests/stubs

USER root
# Without the clients Airflow's image installs for other uses, which nothing here runs, and which bring
# most of the vulnerabilities without a fix yet: ssh, rsync, git, wget, sudo, GnuPG (apt checks signatures
# with gpgv, which stays), the MariaDB, SQLite, LDAP and MS ODBC clients, the Docker CLI, and the libraries
# only they link. SQLite's library too: Python's sqlite3 needs it, but Airflow only imports that for a
# SQLite database, and every container here has Postgres (without a database URL, Airflow defaults to
# SQLite and stops at that import). Not apt's autoremove: dpkg doesn't know what Airflow's own Python
# links (libexpat1, ncurses). Debian's sudo refuses to go while root has no password, unless forced.
RUN SUDO_FORCE_REMOVE=yes apt-get purge --yes openssh-client libfido2-1 libcbor0.8 rsync libpopt0 git \
      git-man liberror-perl libcurl3-gnutls wget sudo gnupg gnupg2 gnupg-l10n gnupg-utils gpg gpg-agent \
      gpg-wks-client gpg-wks-server gpgconf gpgsm dirmngr pinentry-curses libassuan0 libksba8 libnpth0 \
      sqlite3 libsqlite3-0 ldap-utils mariadb-client mariadb-client-core libmariadb3 libmariadb3-compat \
      mariadb-common mysql-common libconfig-inifiles-perl libedit2 msodbcsql18 unixodbc odbcinst \
      libodbcinst2 unixodbc-common docker-ce-cli \
    && rm /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/mariadb.list \
      /etc/apt/sources.list.d/mssql-release.list
# Debian's security updates since Airflow built its image. Only a build that doesn't reuse this layer
# from its cache (--no-cache) picks up later ones.
RUN apt-get update && apt-get upgrade --yes && apt-get clean && rm -rf /var/lib/apt/lists/*
# No pip, in Airflow's venv (~airflow/.local) or in the bare interpreter: nothing in a container installs
# anything (Airflow's /entrypoint only would, for _PIP_ADDITIONAL_REQUIREMENTS), and pip bundles its own
# certifi, urllib3, msgpack and setuptools, older than the fixed ones.
RUN /usr/python/bin/python3 -m pip uninstall --yes --root-user-action=ignore pip \
    && /home/airflow/.local/bin/python -m pip uninstall --yes --root-user-action=ignore pip
# A cluster may run a container as an arbitrary UID with GID 0 (OpenShift does), so everything the
# image's processes write to has to be writable by group 0 (the setgid keeps it that way for what
# they create), and everything they run must not depend on being UID 50000. Same reasoning as
# Airflow's own image:
# https://airflow.apache.org/docs/docker-stack/entrypoint.html#allowing-arbitrary-user-to-run-the-container
RUN mkdir -p /ngt-data && chown airflow:root /ngt-data && chmod 2775 /ngt-data \
    && chmod -R a+rX,go-w /opt/ngt
# The airflow user, by number as in Airflow's own image: a cluster can't check runAsNonRoot on a name.
USER 50000

# Airflow refuses to run as a user without a name, and finds itself under $HOME/.local. An arbitrary
# UID has neither, unless Airflow's entrypoint can add it to /etc/passwd, which a read-only root
# filesystem rules out, and which never reaches what `kubectl exec` starts anyway. Python takes both from
# here instead.
# NGT_DEV_HOME is a subdirectory, not the mount point itself: sim_reset deletes it.
ENV HOME=/home/airflow \
    USER=airflow \
    NGT_AIRFLOW_IN_CONTAINER=1 \
    NGT_SIM_STUB_INSTALLED=1 \
    NGT_DEV_HOME=/ngt-data/demo-env \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/ngt/airflow_automation/airflow_dags \
    PYTHONPATH=/opt/ngt:/opt/ngt/airflow_automation:/opt/ngt/scenario-player \
    PATH=/opt/ngt/airflow_automation/airflow_demo:${PATH}

WORKDIR /opt/ngt

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/opt/ngt/airflow_automation/airflow_containers/container_entrypoint.sh"]
