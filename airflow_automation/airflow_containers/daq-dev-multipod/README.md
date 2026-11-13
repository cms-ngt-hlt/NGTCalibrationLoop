# Several pods on CMS DAQ's daq-dev cluster, with persistent storage

A worked example of putting the [multi-pod deployment](../README.md#several-pods-on-kubernetes-with-persistent-storage)
on `daq-dev`, the CMS DAQ development cluster, in the tenant `ngt-sakura`. That page says what the deployment is and what
it doesn't do. This one says what's different here, and assumes you've read the [one-pod daq-dev page](../daq-dev/README.md),
whose steps 1 to 3 (the tunnel, the Harbor project, the image) are the same.

What `daq-dev` changes, all in [`values.yaml`](values.yaml), on top of the chart's own:

- **Volumes:** Postgres on `longhorn` (3 replicas on the workers), the fake data and logs on `trident-csi-nfs-dedicated`
  (NetApp's NFS, the only class whose volumes pods on different nodes can share).
- **Images from `harbor.cms`:** the same Airflow image as the one-pod's, and Postgres through Harbor's cache of Docker Hub.
- **An Ingress** at `https://<namespace>.daq-dev.cms.cern.ch`, named after the namespace, so that every instance has its
  own. It's reachable from the CMS network only, with a certificate from the cluster's cert-manager.
- **The labels and ServiceAccounts** the cluster's Kyverno policies ask for.

Nothing here names the namespace: any of the tenant's (`ngt-sakura-...`) gets its own instance. This page uses
`ngt-sakura-airflow-multipod`, which exists already.

> **Status:** not yet deployed on `daq-dev`. Checked against the cluster, read-only, on 2026-11-13: the StorageClasses
> and quotas below. The rendered output passes Kyverno's upstream versions of the cluster's policies, except
> `forbid-cpu-limits` as for the one-pod. It's tested on kind as the generic page says. Writing to NetApp's NFS
> volumes as the image's user is the step most likely to need adjusting. If a step differs, please correct this page.

## Before you start

- Everything in the one-pod page's [Before you start](../daq-dev/README.md#before-you-start), and its steps
  [1](../daq-dev/README.md#1-connect-to-the-cluster) (connect), [2](../daq-dev/README.md#2-a-harbor-project-for-the-image)
  (Harbor) and [3](../daq-dev/README.md#3-build-and-push-the-image) (the image) done. If the one-pod runs already, they
  are.
- Helm 4 (`helm version`), with the tunnel's `proxy-url` in your kubeconfig, as for `kubectl`.

Then, in the shell with the tunnel's `KUBECONFIG`:

```bash
export NS=ngt-sakura-airflow-multipod
kubectl create namespace $NS          # skip if it exists; any name starting with ngt-sakura-
```

## 1. The Secret, once

```bash
pw=$(openssl rand -hex 16)
kubectl -n $NS create secret generic ngt-airflow \
  --from-literal=postgres-password="$pw" \
  --from-literal=connection="postgresql+psycopg2://airflow:$pw@ngt-airflow-postgres:5432/airflow" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=api-secret-key="$(openssl rand -hex 32)" \
  --from-literal=fernet-key="$(openssl rand -base64 32 | tr '+/' '-_')" \
  --from-literal=admin-password="$(openssl rand -hex 16)"
unset pw
# Only if the Harbor project is private, as in the one-pod page's step 4:
kubectl -n $NS create secret docker-registry harbor-ngt-sakura --docker-server=harbor.cms \
  --docker-username='robot$ngt-sakura+pull' --docker-password='<its token>'
```

Keep it for as long as the volumes: Postgres keeps the password it was first set up with. Every member of the tenant's
owner group can read it.

## 2. Install

```bash
helm repo add apache-airflow https://airflow.apache.org                                 # once
helm dependency build airflow_automation/airflow_containers/multipod-helm               # Airflow's chart into charts/
helm plugin install airflow_automation/airflow_containers/multipod-helm/post-renderer   # once per machine
helm install ngt airflow_automation/airflow_containers/multipod-helm -n $NS \
  -f airflow_automation/airflow_containers/daq-dev-multipod/values.yaml \
  --post-renderer ngt-airflow-patches --wait --timeout 15m
kubectl -n $NS get pods   # six, all 1/1 Running
kubectl -n $NS get pvc    # three, all Bound
```

`helm` goes through the tunnel like `kubectl`, from the same kubeconfig. Only `repo add` and `dependency build` go to the
internet, for Airflow's chart.

## 3. Check the cluster's policy report

```bash
kubectl -n $NS get policyreport
```

Expect `forbid-cpu-limits` as a warning on every workload, as for the one-pod (see its
[step 5](../daq-dev/README.md#5-check-the-clusters-policy-report)), and possibly `block-large-images` errors. Anything
else is new, and worth fixing here. The `emptyDir` failure the one-pod has doesn't apply: its data is on volumes.

## 4. The UI, a scenario

```bash
kubectl -n $NS get secret ngt-airflow -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n $NS exec deploy/ngt-airflow-tools -- airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
```

The UI is at `https://<namespace>.daq-dev.cms.cern.ch`, here `https://ngt-sakura-airflow-multipod.daq-dev.cms.cern.ch`,
from a browser set up as in the one-pod page's [step 6](../daq-dev/README.md#6-open-the-ui). Or with
`kubectl -n $NS port-forward svc/ngt-api-server 8099:8080`, at `http://localhost:8099`. The login is `admin` with that
password.

## 5. Reset, upgrade, remove

As in the [generic page's step 5](../README.md#5-reset-upgrade-remove), with `-n $NS`, and step 2's `helm` arguments for
an upgrade. The demo's resets stop Airflow's components around them:

```bash
kubectl -n $NS scale deploy -l tier=airflow --replicas=0
kubectl -n $NS exec deploy/ngt-airflow-tools -- airflow_demo.sh reset-scenario
kubectl -n $NS exec deploy/ngt-airflow-tools -- airflow_demo.sh reset-watch
kubectl -n $NS scale deploy -l tier=airflow --replicas=1
```

`helm uninstall ngt -n $NS` removes the pods but keeps the three volumes, and with them the history. A later
`helm install ngt` in the same namespace takes them back. `kubectl delete namespace $NS` removes everything: Longhorn's
and NetApp's classes here delete a volume with its claim.

**A second instance:** another namespace, then the commands above with `NS` set to it. It gets its own database, data,
login and host, e.g. `https://ngt-sakura-airflow-demo2.daq-dev.cms.cern.ch`.

## If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| The scheduler's `setup` or the triggerer fails with `Permission denied` under `/ngt-data` | The NFS volume isn't writable for the image's user (UID 50000, GID 0) | `kubectl -n $NS logs deploy/ngt-scheduler -c setup`; ask the sysadmins about the class's export permissions. Meanwhile the data and logs can go on `longhorn`, with all the pods on one node (see below) |
| A PVC `Pending` after step 2 | Quota, or the class can't make the volume | `kubectl -n $NS describe pvc`; `kubectl describe resourcequota` |
| Pods `CreateContainerConfigError` | No Secret `ngt-airflow` in this namespace | Step 1 |
| `found in Chart.yaml, but missing in charts/ directory: airflow` | Airflow's chart isn't downloaded yet | Step 2's `helm dependency build` |
| `Error: plugin "ngt-airflow-patches" not found` | The post-renderer isn't installed on this machine | Step 2's `helm plugin install` |
| `helm repo add` or `dependency build` times out | The machine has no internet: Airflow's chart comes from airflow.apache.org | Run Helm where there is, still through the tunnel |
| Two instances, one UI | Two namespaces can't share a host, and here they never do | `kubectl get ingress -A` |
| Anything about the tunnel, kubeconfig, Harbor or the CA | As for the one-pod | [Its table](../daq-dev/README.md#if-something-goes-wrong) |

If NetApp's NFS doesn't work out, the shared volumes can be Longhorn's too. Its volumes are `ReadWriteOnce`, so the
scheduler, triggerer, API server and tools pods would all have to run on one node. That's still persistent, but they no
longer spread over the cluster.

## What's different from other clusters

- **The storage, as read from the cluster on 2026-11-13.**
  - `longhorn`: 3 replicas, expandable.
  - `trident-csi-nfs-dedicated`: NetApp `ontap-nas`, expandable. It makes a volume only once a pod uses it.
  - Both use `reclaimPolicy: Delete`.
  - The tenant's quotas: 200Gi of `longhorn` and 50Gi of NFS, at most 30Gi per NFS volume. This deployment asks for 5Gi
    and 4Gi.
- **Dev volumes can be lost**, as `rancher-dev.md` warns ("Namespaces and Longhorn volumes can be lost"): this
  persistence survives restarts, node drains and reinstalls, not the cluster being rebuilt.
- **The rest is as for the one-pod:** [its list](../daq-dev/README.md#whats-different-from-other-clusters).
