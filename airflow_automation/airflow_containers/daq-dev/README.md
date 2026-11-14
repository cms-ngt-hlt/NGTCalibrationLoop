# One pod on CMS DAQ's daq-dev cluster

A worked example of putting the [one-pod deployment](../README.md#one-pod-on-kubernetes) on one particular cluster:
`daq-dev`, the CMS DAQ development cluster managed through Rancher, in the tenant `ngt-sakura`. The UI ends up at
`https://ngt-sakura-airflow.daq-dev.cms.cern.ch`, which only exists **inside the CMS network**: anyone there can look at
it, and changing anything takes a login. No Kubernetes experience is assumed. That page says what the deployment is and
[what it doesn't do](../README.md#what-this-deployment-doesnt-do); this one, what's different here. The cluster's own
documentation is the [CMS DAQ sysadmins' public docs](https://gitlab.cern.ch/cms-daq-sysadmins/public-docs): trust its
`kubernetes/rancher/` pages over this one where they disagree.

`kustomization.yaml` puts this on top of [`../onepod/`](../onepod/), for `kubectl apply -k`:

- **The namespace `ngt-sakura-airflow`.** A tenant's namespaces must start with its name.
- **Images from `harbor.cms`**, the only registry allowed besides `gitlab-registry.cern.ch`: ours in a Harbor project,
  and Postgres through Harbor's cache of Docker Hub.
- **An Ingress** for the UI, on the cluster's Traefik, with a certificate from its cert-manager. It terminates TLS, so
  the login's cookies are set to https only.
- **Its own ServiceAccount and the labels the cluster asks for**, which its Kyverno policies check.

> **Status:** deployed on `daq-dev` on 2026-11-07, following this page, with the image `3.3.1-1`. Both containers became
> ready with no restarts. Postgres came through Harbor's cache as in step 3. The certificate came from the cluster's
> cert-manager, and the UI and Airflow's health check answered through the Ingress. Its policy report differed from what
> this page first expected, and step 5 now says what it showed. The rules below come from the tenant's own settings, read
> on 2026-11-06. If a step differs for you, please correct this page.

## Before you start

- **Membership of the tenant's owner group**, `cern-cms-ngt-hlt-task34`. Its members administer every namespace of the
  tenant, and nothing else on the cluster.
- **A way into the CMS network:** `ssh cmsusr` working, from outside CERN through LXPLUS (`ProxyJump`).
- **The kubeconfig**: [Rancher](https://rancher-dev.cms.cern.ch), log in, the `daq-dev` cluster, *Download KubeConfig*.
  Its token lasts 30 days.
- **`kubectl` within one minor version of the cluster**, which runs Kubernetes 1.36. Older ones mostly work, but aren't
  supported.
- **Docker** to build the image, and [**crane**](https://github.com/google/go-containerregistry/blob/main/cmd/crane/README.md)
  to push it: a single binary, which goes through the tunnel below with nothing but environment variables.
- A checkout of this repo.

## 1. Connect to the cluster

The cluster's API, Harbor and the UI are all inside the CMS network, so everything goes through a SOCKS tunnel. Leave
this running in a terminal of its own (any free port will do, as long as it's the same everywhere below):

```bash
ssh -D 1080 -N cmsusr
```

Then point the kubeconfig at the tunnel, and select the namespace you'll create in step 3:

```bash
export KUBECONFIG=~/daq-dev.yaml            # wherever you saved it
for cluster in rancher daq-dev daq-dev-fqdn; do kubectl config set-cluster $cluster --proxy-url=socks5://localhost:1080; done
kubectl config use-context daq-dev-fqdn     # the API server directly; `daq-dev` goes through Rancher
kubectl get all -n ngt-sakura-default       # "No resources found": you're in
```

`daq-dev-fqdn` is what this page was checked with. It refuses cluster-wide listings like `kubectl get ns` (403), which
work on `daq-dev`.

The cluster's certificates, Harbor's and the UI's are all signed by the CMS Puppet CA (`puppet8-ca.cms`). The kubeconfig
carries it, so save it for the steps below:

```bash
kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="rancher")].cluster.certificate-authority-data}' | base64 -d > cms-puppet-ca.pem
```

## 2. A Harbor project for the image

Log in at [`https://harbor.cms`](https://harbor.cms), through the tunnel (see [step 6](#6-open-the-ui) for the browser),
with your CMS account. The overlay expects a project called **`ngt-sakura`**, like the tenant. If *+ New Project* isn't
there for you, ask the sysadmins for it: a [CMSONS ticket](https://its.cern.ch/jira/projects/CMSONS), component
Kubernetes.

- **Public** is simplest: anyone in the CMS network can pull it, and the cluster needs no credentials. The image holds
  nothing secret.
- **Private** needs a robot account with pull permission (project -> Robot Accounts), whose token goes in a Secret in
  step 3.

To push, Harbor wants your username and **CLI secret** (top right -> User Profile), not your password, as it logs you in
through Keycloak. A robot account with push permission works too.

## 3. Build and push the image

From the root of your checkout:

```bash
docker build -f airflow_automation/airflow_containers/Dockerfile -t ngt-airflow:3.3.1-5 .
docker save ngt-airflow:3.3.1-5 -o ngt-airflow.tar

export HTTPS_PROXY=socks5://localhost:1080 SSL_CERT_FILE=$PWD/cms-puppet-ca.pem
crane auth login harbor.cms -u <your Harbor username> --password-stdin    # paste the CLI secret, then Ctrl-D
crane push ngt-airflow.tar harbor.cms/ngt-sakura/ngt-airflow:3.3.1-5
crane ls harbor.cms/ngt-sakura/ngt-airflow                                 # 3.3.1-5
unset HTTPS_PROXY SSL_CERT_FILE
```

`docker push` would work too, but only after setting the Docker daemon's own proxy and CA. Harbor scans every image it
receives for known vulnerabilities, and Airflow's images tend to have some. The project's settings decide whether that
stops it being pulled. The image takes the fixes released by the time it's built; `docker build --no-cache` gets the
ones released since.

**Each new build needs a new tag**: `3.3.1-6`, and so on, also as `newTag` in `kustomization.yaml`. The cluster keeps
the images it has pulled, and won't fetch a tag again that it already has.

## 4. Create the namespace and deploy

```bash
kubectl create namespace ngt-sakura-airflow
kubectl config set-context --current --namespace=ngt-sakura-airflow
# Only if the Harbor project is private, with the robot account from step 2:
kubectl create secret docker-registry harbor-ngt-sakura --docker-server=harbor.cms \
  --docker-username='robot$ngt-sakura+pull' --docker-password='<its token>'

kubectl apply -k airflow_automation/airflow_containers/daq-dev/
kubectl rollout status deploy/ngt-airflow    # a few minutes the first time, as both images are pulled
kubectl get pods                             # 2/2 Running
```

## 5. Check the cluster's policy report

```bash
kubectl get policyreport
```

The cluster's Kyverno policies only report for now, but will block on staging and production. Expect a warning and a
failure, both known:

- **`forbid-cpu-limits`**, a warning, on every object that holds the pod's spec. The pod keeps its CPU limits from when
  the tenant gave a container without one a limit of 1 CPU, which would have halved Airflow's. By 2026-11-14 it no
  longer did, so they could go.
- **`restrict-emptydir-sizelimit`**, a failure on the Pod: the cluster wants every `emptyDir` under 400Mi, and this pod
  keeps its database (1Gi) and its data (2Gi) on `emptyDir`s. Smaller ones would have to be measured first.
- **`block-large-images`** can show an error instead of a result, when Kyverno fails to read the image from Harbor, as
  it did for a test pod on 2026-11-06. That's for the sysadmins.

Anything else is new, and worth fixing here.

## 6. Open the UI

```bash
kubectl exec deploy/ngt-airflow -c airflow -- bash -c 'cat $AIRFLOW_HOME/admin_password'
```

- **From a browser at the address:** in Firefox, Settings -> Network Settings -> Manual proxy configuration: SOCKS host
  `localhost`, port `1080`, SOCKS v5, and tick *Proxy DNS when using SOCKS v5*. Then Settings -> Certificates -> View
  Certificates -> Authorities -> Import `cms-puppet-ca.pem`, trusted for websites. Open
  `https://ngt-sakura-airflow.daq-dev.cms.cern.ch`. On a machine inside the CMS network, just the address.
- **Or with nothing to set up:** `kubectl port-forward svc/ngt-airflow 8099:8080`, then `http://localhost:8099`.

Anyone in the CMS network can look at the UI, with no login. To change anything, log in at `/auth/login/` (the Home page
links to it) as `admin`, with that password, and keep it to yourself. A new pod generates a new one.

## 7. Play a scenario

```bash
kubectl exec deploy/ngt-airflow -c airflow -- airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
```

The tunnel has to stay up for the 2-3 minutes it takes. The rest is as on any cluster:
[guided runs, and resetting between scenarios](../README.md#4-play-a-scenario).

## 8. Reset, stop, remove

```bash
kubectl rollout restart deploy/ngt-airflow     # reset: a new, empty pod
kubectl scale deploy/ngt-airflow --replicas=0  # stop, losing all state; --replicas=1 starts it again
kubectl delete namespace ngt-sakura-airflow    # remove everything, the Secret and the certificate too
```

## If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `invalid character '<' looking for beginning of value`, or `failed to download openapi: proto: cannot parse invalid wire-format data` | Something other than the API answered: `kubectl` went to `localhost:8080`, because this shell has no `KUBECONFIG` (a new terminal, a restarted WSL), or a context has no `cluster` | Step 1's `export KUBECONFIG=...` again; `kubectl config current-context`; `kubectl config view`: each context names its cluster, each cluster has the tunnel's `proxy-url` |
| `proxyconnect tcp: ... connection refused` | The tunnel is down, or on another port | Step 1's `ssh -D` |
| `Unauthorized`, after some weeks | The kubeconfig's token expired (30 days) | Download it again, and redo step 1 |
| 403 on `kubectl get ns` | The `-fqdn` context doesn't list cluster-wide | Use the `daq-dev` context for that |
| Namespace refused | Its name doesn't start with `ngt-sakura-` | Keep the prefix |
| `Forbidden` right after `kubectl create namespace` | Capsule gives the tenant's owners their rights in a new namespace a few seconds after it's created | Wait a few seconds, and run the command again |
| `ImagePullBackOff` on `airflow` | Not pushed, another project or tag, or a private project without step 4's Secret | `kubectl describe pod`; `crane ls` as in step 3 |
| Warning `FailedToRetrieveImagePullSecret` | There's no `harbor-ngt-sakura` Secret | Nothing, if the project is public |
| `ImagePullBackOff` on `postgres` | Harbor's cache path changed | `kubectl describe pod`; ask the sysadmins for the path of Docker Hub's `library/postgres` |
| `apply` works, but no pod appears | The pod was refused, e.g. for its registry | `kubectl describe rs`: the refusal is in its events |
| Pod `Pending`, `exceeded quota` | The tenant's quota, shared by all its namespaces, is full | `kubectl describe resourcequota` |
| `x509: certificate signed by unknown authority` | The CMS Puppet CA isn't trusted | `SSL_CERT_FILE` for crane, step 6 for the browser |
| `exec` or `port-forward` hangs | Its connection doesn't make it through the tunnel | Prefix it with `KUBECTL_REMOTE_COMMAND_WEBSOCKETS=false` (exec) or `KUBECTL_PORT_FORWARD_WEBSOCKETS=false` |
| `OOMKilled`, restarting, `play` reports nothing | As on any cluster | [The generic table](../README.md#if-something-goes-wrong) |

## What's different from other clusters

Besides [what the deployment doesn't do anywhere](../README.md#what-this-deployment-doesnt-do):

- **The rules, as read from the cluster on 2026-11-06.** Registries `harbor.cms` and `gitlab-registry.cern.ch`; IngressClass
  `traefik`, hostnames matching `.*\.daq-dev\.cms\.cern\.ch`; for the whole tenant, 48 CPU, 128Gi and 30 namespaces. No
  NetworkPolicy and no Pod Security level are set on the tenant's namespace.
- **Per container, as read again on 2026-11-14:** unless it says otherwise, 100m CPU and 512Mi requested, and a 1Gi
  memory limit. At least 40m CPU and 40Mi, at most 64Gi. There's no default CPU limit and no CPU maximum any more: on
  2026-11-06, there were 1 CPU and 32.
- **Every member of the owner group administers everything in the tenant**, this namespace included, and can read its
  Secrets.
- **Nodes get drained for maintenance**, and each time this demo comes back empty.
