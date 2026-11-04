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

What you end up with: the prototype running in one pod on a Kubernetes cluster, its UI behind a login. The pod
holds two containers: Airflow, with all its components under one `airflow standalone`, and a Postgres next to it.
Everything is ephemeral: **deleting the pod deletes all state**, which is also how you reset it. It's a demo with
fake data, not a way to run the real pipeline.

[`onepod/ngt-airflow-onepod.yaml`](onepod/ngt-airflow-onepod.yaml) is plain Kubernetes, a Deployment and a Service,
and meets the `restricted` [Pod Security level](https://kubernetes.io/docs/concepts/security/pod-security-standards/).
What depends on your cluster is left to you: how the image gets there, and how the UI gets exposed.

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
docker compose exec airflow bash -c 'cat $AIRFLOW_HOME/simple_auth_manager_passwords.json.generated'
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
kubectl exec deploy/ngt-airflow -c airflow -- bash -c 'cat $AIRFLOW_HOME/simple_auth_manager_passwords.json.generated'
```

Log in with one of the two generated accounts:

- `viewer` can look at everything and change nothing: the one to hand out.
- `admin` can do anything, e.g. trigger DAG runs: keep it to yourself.

A new pod generates new passwords.

To show it to other people, expose the Service the way your cluster does that: an Ingress, a Gateway, a LoadBalancer.
Mind who that lets in, as this login is all that stands in front of the UI ([see below](#what-this-deployment-doesnt-do)).

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
the retries are over, many minutes later, and fail its expectations: `sequential_runs` after
`partial_to_total_fault_injection_demo` does.

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
| `airflow` restarting, liveness probe failed | A component died: `airflow standalone` doesn't restart them, so the probe restarts the container | `kubectl logs deploy/ngt-airflow -c airflow --previous`: each line starts with the component's name |
| `play` reports nothing happens | Run number already played in this pod | Reset |
| `play` fails, a calibration uploaded nothing | An earlier scenario's run is still retrying | Reset: see [step 4](#4-play-a-scenario) |
| The UI's Assets view shows an error for a moment | Airflow's bug, in 3.3.1 and 3.3.2: `GET /api/v2/assets/events` answers 500 while an event's triggered run is still queued | Nothing, it passes |

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

[`openshift/`](openshift/README.md) is a worked example, for CERN's OpenShift.

### What this deployment doesn't do

- **Keep anything.** No persistent storage, on purpose: nothing here is worth keeping, and it needs no storage request.
  Whatever moves the pod, such as a node drain, leaves you with a new, empty one.
- **Survive load.** One replica, with one `LocalExecutor` on 8 worker processes.
- **Authenticate properly.** Airflow's Simple Auth Manager is meant for development; it's used here because there's
  nothing real behind this login. Don't reuse this setup for anything with real credentials.
- **Run the real pipeline.** That needs real OMS, EOS, CMSSW and conditions DB access: see README.md's "Deploying on the
  calibration nodes".
