# Running the Airflow prototype in containers

For anyone who wants the prototype running somewhere other than their own WSL: a colleague trying it out, or
someone showing its UI to other people from a Kubernetes cluster. You need a shell; no Airflow or Kubernetes
experience is assumed.

All of these run the same thing as `airflow_demo.sh` on a bare host (see the repo's README.md): the real
Airflow DAGs against the fake OMS/EOS/CMSSW toolchain. No real data, no real uploads.

| You want to... | Use | Needs |
|---|---|---|
| Develop DAGs and tests quickly, no image builds | [`../airflow_demo/airflow_demo.sh`](../airflow_demo/airflow_demo.sh) | Airflow venv, Postgres, tmux |
| Run it with one container per Airflow component, as production would | [Separate containers](#separate-containers-on-one-machine) (`compose/`) | Docker with Compose v2 |
| Show the UI to other people, with the least effort | [One pod on Kubernetes](#one-pod-on-kubernetes) (`onepod/`) | a Kubernetes cluster, `kubectl`, Docker |
| The same on CERN's OpenShift, as a worked example | [`openshift/`](openshift/README.md) | a CERN account |
| The same on CMS DAQ's `daq-dev` cluster, as another | [`daq-dev/`](daq-dev/README.md) | a CMS account, a tenant there |
| Keep its history, with one pod per Airflow component | [Several pods on Kubernetes](#several-pods-on-kubernetes-with-persistent-storage) (`multipod-helm/`) | a cluster with shareable volumes, `kubectl`, Helm 4, Docker |
| The same on `daq-dev` | [`daq-dev-multipod/`](daq-dev-multipod/README.md) | a CMS account, a tenant there |

All of them come from one image, [`Dockerfile`](Dockerfile): Airflow 3.3.1 plus this repo at `/opt/ngt`.

## Separate containers, on one machine

API server, scheduler, dag-processor and triggerer each in their own container, plus Postgres and a one-shot
init: the layout Airflow's deployment docs describe. It's Airflow's own `docker-compose.yaml` trimmed to
LocalExecutor (no Redis or Celery workers, which only exist to spread tasks over several machines).

```bash
# Everything from nothing: builds the image, starts it all, plays the scenario, checks its result.
./airflow_automation/airflow_containers/compose/airflow_compose.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml

./airflow_automation/airflow_containers/compose/airflow_compose.sh ui      # the UI's address; no login needed
./airflow_automation/airflow_containers/compose/airflow_compose.sh down    # remove everything, volumes too
```

`airflow_compose.sh` takes the same commands as `airflow_demo.sh`, and adds `build`, `down` and `shell`. Run it
with no arguments for the list. Two things to know:

- **The code is in the image**, so after changing it: `build`, then `start`. Scenario files are the exception:
  `play` takes one from anywhere on your machine, as is.
- **A guided walkthrough** is `play <scenario> --interactive --monitor`, the same as on a bare host.
  `--monitor` keeps watching after the scenario ends, until you press Enter or Ctrl-C.

Every container runs the strictest way a Kubernetes cluster can run it, an arbitrary non-root user with no
capabilities, and mounts only what its component needs: so if it works here, those two ways it could fail on a
cluster are already ruled out. The UI is published on `127.0.0.1` only, since it has no login by default. For
real logins, set `NGT_AIRFLOW_ALL_ADMINS=False` and read them with `airflow_compose.sh logins`.

Per-checkout settings work as for `airflow_demo.sh`: several checkouts get separate stacks, and the settings that
exist only here go in `local_airflow_env.sh` too (see its `.example`). The generated secrets are in `compose/.env`
(gitignored): don't delete it while a stack's volumes exist, because the database's contents depend on it.

Tests: `pytest tests/airflow-ngt/test_airflow_containers.py` needs no Docker; `pytest -m container` plays a
scenario against this stack and against the one-pod emulation, in a few minutes.

## One pod on Kubernetes

What you end up with: the prototype running in one pod on a Kubernetes cluster. Anyone who can reach its UI can look at
it, and changing anything takes logging in as its Admin. The pod holds two containers: Airflow, with all its components,
and a Postgres next to it. Everything is ephemeral: **deleting the pod deletes all state**, which is also how you reset
it. It's a demo with fake data, not a way to run the real pipeline.

[`onepod/ngt-airflow-onepod.yaml`](onepod/ngt-airflow-onepod.yaml) is plain Kubernetes, a Deployment and a Service,
and meets the `restricted` [Pod Security level](https://kubernetes.io/docs/concepts/security/pod-security-standards/).
Its root filesystems are read-only: whatever it writes goes to its emptyDirs. What depends on your cluster is left to
you: how the image gets there, and how the UI gets exposed.

> **Status:** verified on Docker and on a Kubernetes (kind) cluster enforcing that level, with a scenario played.

### Before you start

- A Kubernetes cluster, with `kubectl` pointing at a namespace you may use. To try it on your own machine,
  [kind](https://kind.sigs.k8s.io) is enough.
- Docker, to build the image, and a registry your cluster can pull from (a local cluster doesn't need one).
- A checkout of this repo.

### Try it locally first (5 minutes, optional)

`onepod/docker-compose.yaml` runs the same two containers with the same settings and restrictions, no cluster needed:

```bash
cd airflow_automation/airflow_containers/onepod
docker compose up -d --build
docker compose exec airflow airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
# ...ends with "expectations: PASSED". The UI is at http://localhost:8099 (NGT_ONEPOD_PORT).
docker compose exec airflow bash -c 'cat $AIRFLOW_HOME/admin_password'
docker compose down -v
```

### 1. Build the image and get it to your cluster

From the root of your checkout:

```bash
docker build -f airflow_automation/airflow_containers/Dockerfile -t <registry>/ngt-airflow:1 .
docker push <registry>/ngt-airflow:1
```

A local cluster needs no registry: build it as `ngt-airflow:1`, then `kind load docker-image ngt-airflow:1` or
`minikube image load ngt-airflow:1`. If `kind load` fails with `content digest ... not found`, as it can with Docker's
containerd image store:
`docker save --platform linux/amd64 ngt-airflow:1 | docker exec -i kind-control-plane ctr -n k8s.io images import --platform linux/amd64 -`.

### 2. Create everything

```bash
kubectl apply -f airflow_automation/airflow_containers/onepod/ngt-airflow-onepod.yaml
kubectl set image deploy/ngt-airflow airflow=<registry>/ngt-airflow:1
kubectl rollout status deploy/ngt-airflow    # about a minute
kubectl get pods                             # 2/2 Running
```

The manifest names a placeholder image, so `ImagePullBackOff` is expected until `set image`, and applying the
manifest again brings the placeholder back. The pod requests 400m CPU and 1.1 GiB of memory, with limits of 2.5 CPU
and 2.25 GiB, and used about 1.5 GiB of memory at its peak.

### 3. Open the UI

```bash
kubectl port-forward svc/ngt-airflow 8099:8080     # leave it running: the UI is at http://localhost:8099
kubectl exec deploy/ngt-airflow -c airflow -- bash -c 'cat $AIRFLOW_HOME/admin_password'
```

- **Anyone who reaches the UI can look at it**, with no login: DAGs, runs, task logs and code, but not connections or
  variables. Buttons that would change something are still there, and answer "Access Denied".
- **To change anything**, e.g. to trigger a DAG run, log in at `/auth/login/` (the Home page links to it) as `admin`, with
  that password. Keep it to yourself. A new pod generates a new one. Under Security -> Users, `admin` can add others.

To show it to other people, expose the Service the way your cluster does that: an Ingress, a Gateway, a LoadBalancer.
Mind who that lets in, since they can all read it. To have them log in for that too, set `AIRFLOW__FAB__AUTH_ROLE_PUBLIC`
to `""`, and give them users. If what you expose it with terminates TLS, set `AIRFLOW__FAB__ENABLE_PROXY_FIX` and
`AIRFLOW__FAB__COOKIE_SECURE` to `"True"`, so that the login's cookies are for https only. See also
[below](#what-this-deployment-doesnt-do).

### 4. Play a scenario

The DAGs are already unpaused and their watchers running, waiting for a run:

```bash
kubectl exec deploy/ngt-airflow -c airflow -- airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
```

Watch the DAGs in the UI as it plays (2-3 minutes). It ends with `expectations: PASSED` and exit code 0. For a guided
one, with `-it` after `kubectl exec`: `... play scenario-player/scenarios/fault_injection_demo.yaml --interactive --monitor`.
The other scenarios are in `scenario-player/scenarios/`; the image has its own copy of the ones you built with.

**Reset between scenarios.** A run number works once per pod (the watchers remember it). And each calibration's file
watcher serves only its highest open run, where a run with a file still retrying counts as open. The scenarios' run
numbers aren't in order, so one played right after a scenario with faults can get nothing for that calibration until
the retries are over, many minutes later, and fail its expectations.

### 5. Reset, stop, remove

```bash
kubectl rollout restart deploy/ngt-airflow     # reset: a new, empty pod
kubectl scale deploy/ngt-airflow --replicas=0  # stop, losing all state; --replicas=1 starts it again
kubectl delete -f airflow_automation/airflow_containers/onepod/ngt-airflow-onepod.yaml   # remove everything
```

### If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `ImagePullBackOff` on `airflow` | Still the placeholder image, or your cluster can't pull yours | Step 2's `kubectl set image`; `kubectl describe pod` |
| `ImagePullBackOff` on `postgres` | Docker Hub unreachable, or rate-limiting your cluster | Use a mirror: `kubectl set image deploy/ngt-airflow postgres=<mirror>/postgres:16` |
| Pod rejected over its `runAsUser` | The cluster assigns UIDs itself, and refuses the one `postgres` asks for | Remove it in an [overlay](#adapting-it-to-your-cluster): the image works as any UID |
| Pod `Pending`, `exceeded quota` | The namespace's quota is too small | `kubectl describe quota`; lower the `requests` in the manifest |
| `airflow` restarting, `OOMKilled` | Memory limit too low | `kubectl describe pod`; raise the limit, or lower `AIRFLOW__CORE__PARALLELISM` |
| `airflow` restarting | A component died, which takes the container with it, or hung, which fails the liveness probe | `kubectl logs deploy/ngt-airflow -c airflow --previous`: each line starts with the component's name |
| `play` reports nothing happens | Run number already played in this pod | Reset |
| `play` fails, a calibration uploaded nothing | An earlier scenario's run is still retrying | Reset: see [step 4](#4-play-a-scenario) |
| The UI's Assets view shows an error for a moment | Airflow's bug, in 3.3.1 and 3.3.2: `GET /api/v2/assets/events` answers 500 while an event's triggered run is still queued | Nothing, it passes |
| The log has a `ValidationError` for `AuthenticatedMeResponse`, at each page someone opens without logging in | Airflow's bug: `GET /ui/auth/me` answers 500 for an anonymous user | Nothing, the UI does without it |

`kubectl logs deploy/ngt-airflow -c airflow` shows all four components, `kubectl exec -it deploy/ngt-airflow -c airflow -- bash`
gets you a shell.

### Adapting it to your cluster

If your cluster needs more than `kubectl set image`, keep that in a directory of its own, as a
[kustomize](https://kustomize.io) overlay on `onepod/`, which `kubectl` understands by itself. Your own image, a mirror
for Postgres, an Ingress or whatever else your cluster wants all go there, and the manifest stays the same for everyone:

```yaml
# my-cluster/kustomization.yaml, next to onepod/. Then: kubectl apply -k my-cluster/
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../onepod
images:
  - name: ngt-airflow
    newName: <registry>/ngt-airflow
    newTag: "1"
```

[`openshift/`](openshift/README.md) is a worked example, for CERN's OpenShift, and [`daq-dev/`](daq-dev/README.md)
another, for CMS DAQ's `daq-dev` cluster.

### What this deployment doesn't do

- **Keep anything.** No persistent storage, on purpose: nothing here is worth keeping, and it needs no storage request.
  Whatever moves the pod, such as a node drain, leaves you with a new, empty one.
- **Survive load.** One replica, with one `LocalExecutor` on 8 worker processes.
- **Authenticate people.** One shared Admin password, on a UI anyone can read, is only fine because there's nothing real
  behind it. Don't reuse this setup for anything with real credentials.
- **Run the real pipeline.** That needs real OMS, EOS, CMSSW and conditions DB access: see README.md's "Deploying on the
  calibration nodes".

## Several pods on Kubernetes, with persistent storage

What you end up with: the prototype with one pod per Airflow component (API server, scheduler, dag-processor,
triggerer), Postgres in a pod of its own, and a pod to drive the demo from. Airflow's history, the fake data and the
task logs are on persistent volumes, so they survive any pod being restarted or moved, and Airflow being reinstalled. As
with the one pod, anyone who reaches the UI can read it, and changing anything takes the admin login. It's still a demo
with fake data.

[`multipod-helm/`](multipod-helm/) is a Helm chart with [Airflow's own](https://airflow.apache.org/docs/helm-chart/stable/index.html),
version 1.22.0, as its dependency, which is how Airflow's docs suggest extending theirs:

- **`values.yaml`**: under `airflow:`, Airflow's chart's settings. That's LocalExecutor, the `restricted` Pod Security level
  with read-only root filesystems, no Jobs, nothing that talks to Kubernetes, and resources from what each component used
  at its peak. Above it, the settings of this chart's own objects.
- **`templates/`**: what Airflow's chart doesn't deploy:
  - Postgres and its volume, since the chart's production guide advises a database of your own.
  - The volumes the components share, for the fake data and the logs.
  - The tools pod.
  - The setup script the scheduler runs first (`files/init_multipod.sh`).

  The volumes carry `helm.sh/resource-policy: keep`, so `helm uninstall` leaves them, and the history with them.
- **`post-renderer/`**: a Helm 4 plugin for what the chart's values can't set. Two things policy engines such as Kyverno
  ask for: each pod saying itself that it gets no API token, and an API server whose liveness check isn't its readiness
  check. And, with step pods ([below](#steps-in-pods-of-their-own)), the API token the scheduler needs to start them.

Nothing names the namespace: each namespace you install it in gets an instance of its own.

> **Status:** verified on a Kubernetes (kind) cluster enforcing the `restricted` level, with two instances side by side.
> Scenarios passed, before and after every pod was deleted at once, and after a reset. Data survived `helm uninstall`,
> and a new `helm install` took the volumes back. Also on three workers, with an `airflow.nodeSelector` and volumes
> that start out writable by root alone.

### Before you start

- A Kubernetes cluster, with `kubectl` pointing at a namespace you may use. It needs two kinds of storage: a
  StorageClass for Postgres, and one whose volumes pods on different nodes can share (`ReadWriteMany`, such as NFS or
  CephFS), for the fake data and the logs. Ask your cluster's admins which classes those are, and whether every node can
  mount them. On a one-node cluster, see [below](#on-a-one-node-cluster).
- Helm 4 (`helm version`): Airflow's chart needs 3.19 or later, and the post-renderer is a Helm 4 plugin.
- Docker, a registry your cluster can pull from, and a checkout of this repo, as for the one pod.

### 1. Build the image

As for the one pod: [its step 1](#1-build-the-image-and-get-it-to-your-cluster). Every pod but Postgres's runs that one
image.

### 2. Create the Secret, once

```bash
pw=$(openssl rand -hex 16)
kubectl create secret generic ngt-airflow \
  --from-literal=postgres-password="$pw" \
  --from-literal=connection="postgresql+psycopg2://airflow:$pw@ngt-airflow-postgres:5432/airflow" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=api-secret-key="$(openssl rand -hex 32)" \
  --from-literal=fernet-key="$(openssl rand -base64 32 | tr '+/' '-_')" \
  --from-literal=admin-password="$(openssl rand -hex 16)"
unset pw
```

All the components share these. Keep the Secret for as long as the volumes: Postgres keeps the password it was first set
up with, and the database's contents depend on the Fernet key.

### 3. Install

Your cluster's settings go in a values file of your own, next to the chart's:

```yaml
# my-cluster.yaml
sharedVolumes:
  data:
    storageClassName: <your ReadWriteMany class>
  logs:
    storageClassName: <your ReadWriteMany class>
airflow:
  images:
    airflow:
      repository: <registry>/ngt-airflow
      tag: "1"
```

```bash
helm repo add apache-airflow https://airflow.apache.org
helm dependency build airflow_automation/airflow_containers/multipod-helm               # Airflow's chart into charts/
helm plugin install airflow_automation/airflow_containers/multipod-helm/post-renderer   # once per machine
helm install ngt airflow_automation/airflow_containers/multipod-helm -f my-cluster.yaml \
  --post-renderer ngt-airflow-patches --wait --timeout 15m
kubectl get pods    # six, all 1/1 Running
```

The release must be called `ngt`: the post-renderer expects the names Airflow's chart then gives its objects. The first
start takes two minutes or so. The scheduler's init container migrates the database, sets up the fake data and creates
the `admin` login, while the other components wait for it. Every later start only migrates.

### 4. Open the UI, play a scenario

```bash
kubectl port-forward svc/ngt-api-server 8099:8080      # the UI at http://localhost:8099
kubectl get secret ngt-airflow -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl exec deploy/ngt-airflow-tools -- airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
```

Reading, logging in and playing work as for the [one pod](#3-open-the-ui). `play`, faults and the other `airflow_demo.sh`
commands all run in the tools pod.

### 5. Reset, upgrade, remove

Nothing resets by itself any more: a new pod picks up where the old one stopped. The demo's own resets need Airflow's
components stopped around them:

```bash
kubectl scale deploy -l tier=airflow --replicas=0       # Airflow's four components; Postgres and tools stay
kubectl exec deploy/ngt-airflow-tools -- airflow_demo.sh reset-scenario   # the fake data, and the watchers' memory of it
kubectl exec deploy/ngt-airflow-tools -- airflow_demo.sh reset-watch      # the DAGs' runs (reset-airflow: the whole database)
kubectl scale deploy -l tier=airflow --replicas=1
```

| To... | Run |
|---|---|
| Change a setting, or take a new image | `helm upgrade ngt` with step 3's arguments and the change |
| Remove everything but the history | `helm uninstall ngt`: the three volumes stay, and a later `helm install` in the same namespace takes them back |
| Remove everything, data included | `helm uninstall ngt`, then `kubectl delete pvc ngt-airflow-postgres ngt-airflow-data ngt-airflow-logs` |

A second instance goes in another namespace: steps 2 and 3 with `-n <namespace>` on each command.

### On a one-node cluster

Such as kind or minikube. Their default class's volumes are `ReadWriteOnce`, which on one node every pod can still
share:

```yaml
# my-cluster.yaml
sharedVolumes:
  data:
    accessMode: ReadWriteOnce
  logs:
    accessMode: ReadWriteOnce
```

### Steps in pods of their own

By default the scheduler runs each file's steps itself (LocalExecutor). With `values-step-pods.yaml` added last, steps
2, 3 and 4's harvest each run in a pod of their own instead, which Airflow starts with KubernetesPodOperator and deletes
once it has succeeded:

```bash
helm upgrade ngt airflow_automation/airflow_containers/multipod-helm -f my-cluster.yaml \
  -f airflow_automation/airflow_containers/multipod-helm/values-step-pods.yaml --post-renderer ngt-airflow-patches
kubectl get pods -l app=ngt-airflow-step -w    # while a scenario plays
```

- **What runs in them:** the library's step runner (`ngt_calibration_loop/run_step.py`), with the step's arguments. A
  step hands the next its result through the shared volume.
- **The template:** `stepPods` in `values.yaml`. It uses Airflow's image and its fake toolchain, unless your values give
  another image and the script that sets it up, such as a CMSSW one for real processing. Such an image can have its own
  user (`stepPods.securityContext`) and files beside the script (`stepPods.files`, at `/opt/ngt-step-files`). It finds
  the library on the data volume, at `/ngt-data/lib`, where the scheduler's setup copies Airflow's image's at each start.
- **The scheduler gets what starting them takes:** a Role in the namespace (pods, their logs, events), and an API
  token, which the post-renderer projects in. Without this file it has neither.
- **A failed pod is kept, to look at,** with `kubectl logs <pod>`. Its task in the UI shows the same output.
- **The upload stays in the scheduler.**

### If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `found in Chart.yaml, but missing in charts/ directory: airflow` | Airflow's chart isn't downloaded yet | Step 3's `helm dependency build` |
| A PVC `Pending` | No class can make that kind of volume, or the class waits for a pod to use it (`WaitForFirstConsumer`) | `kubectl describe pvc` |
| Pods stuck in `ContainerCreating`, events `FailedMount` | Their node can't mount the volume, as some nodes can't reach an NFS server | `kubectl describe pod`. Ask your admins which nodes can, and keep the pods there with `airflow.nodeSelector` in your values: the tools pod follows it |
| Pods `CreateContainerConfigError` | No Secret `ngt-airflow` in this namespace | Step 2 |
| The triggerer stuck in `Init` (`Waiting for the scheduler's setup...`) | The scheduler's setup failed | `kubectl logs deploy/ngt-scheduler -c setup` |
| `Permission denied` under `/ngt-data` or `/opt/airflow/logs` | The shared volume isn't writable for the image's user, as some NFS exports aren't | Ask for a class whose volumes any UID can write, or one honouring `fsGroup` |
| `Error: plugin "ngt-airflow-patches" not found` | The post-renderer isn't installed on this machine | Step 3's `helm plugin install` |
| `helm install` without `--post-renderer` worked | It does: only the two policy checks fail again | `helm upgrade` with it |
| Events `Startup probe failed` or `Liveness probe failed: command timed out` in the first minutes | The components are still starting, and the checks run Airflow's CLI, which is slow on a busy node | Nothing, as long as `RESTARTS` stays 0 |
| `PersistentVolumeClaim ... exists and cannot be imported into the current release` | The volumes were left by a release of another name, or in another namespace | Install as `ngt` in their namespace |
| `play` reports nothing happens | The run numbers were already played here | Reset, as in step 5 |
| A step task fails with `pods is forbidden` or `Unauthorized` | Step pods without `values-step-pods.yaml`'s Role or the post-renderer's token | Install with that file, and with `--post-renderer` |
| A step task waits, then fails with `Pod took too long to start` | The step pod can't be scheduled or its image can't be pulled | `kubectl describe pod -l app=ngt-airflow-step` |

Everything else is as for the [one pod](#if-something-goes-wrong). `kubectl logs deploy/ngt-<component>` shows each
component.

### What this deployment doesn't do

- **Spread tasks out.** The scheduler runs them itself (LocalExecutor), in its own pod, except for the steps' own pods
  above. One replica of each component.
- **Back anything up.** The volumes persist, but a cluster that loses them loses the history.
- **Authenticate people**, or **run the real pipeline**: as for the [one pod](#what-this-deployment-doesnt-do).
