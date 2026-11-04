# One pod on CERN's OpenShift

A worked example of putting the [one-pod deployment](../README.md#one-pod-on-kubernetes) on one particular cluster:
CERN's OpenShift (OKD, the "PaaS"), with the UI at an HTTPS address reachable **from the CERN network only**, behind a
login. No OpenShift experience is assumed. That page says what the deployment is and
[what it doesn't do](../README.md#what-this-deployment-doesnt-do); this one, what's different here.

`kustomization.yaml` puts this on top of [`../onepod/`](../onepod/), for `oc apply -k`:

- **An in-cluster image build**, so no registry of your own: an `ImageStream` and a `BuildConfig`
  ([`ngt-airflow-openshift.yaml`](ngt-airflow-openshift.yaml)), and an image trigger that rolls out every new build.
- **A `Route`** for the UI, where another cluster would have an Ingress.
- **CERN's pull-through cache of Docker Hub** for the base image and Postgres, as Docker Hub rate-limits a cluster's
  shared addresses.
- **No fixed UID for `postgres`.** OpenShift's `restricted` security policy (its SCC) assigns a UID from the project's
  own range, and rejects any other. The images work with whichever they get.

> **Status:** checked against the [PaaS guide](https://paas.docs.cern.ch) as of September 2026, but not yet run on
> CERN's cluster. The in-cluster build is the most likely step to need adjusting. If a step differs in reality, trust
> the guide and please correct this page.

## Before you start

- A CERN **primary** account. Only those can own a project.
- A shell on LXPLUS or AIADM, where `oc`, `oc sso-login` and `helm` are installed. The cluster's API isn't reachable from
  outside CERN: `oc` fails with `error: EOF`.
- A checkout of this repo **there**, with what you want to deploy committed, since the build gets `HEAD`. So the branch
  has to be reachable from LXPLUS.
- A session that stays alive for the minutes `play` takes over `oc exec`, e.g. in `tmux`.

To try it first, on your own machine with Docker, see
[Try it locally first](../README.md#try-it-locally-first-5-minutes-optional).

## 1. Create a project

[Web Services Portal](https://webservices-portal.web.cern.ch) -> Create -> PaaS project:

- **Category:** `Test` for a demo. It's blocked after 6 months unless extended, then deleted 180 days later, and can be
  changed later. `Official` once it's a team's lasting demo.
- **Name:** 4-63 characters, not a common word, no `openshift`, `kubernetes` or `kube` prefix, and practically permanent.
  It ends up in the UI's address, as `ngt-airflow-<name>.app.cern.ch`, so keep it short, e.g. `ngt-demo`.
- **Administrator group:** an e-group, so that more than one person can manage it.

For a dry run there's a [playground](https://webservices-portal-dev.web.cern.ch), with
`oc sso-login --server https://api.paas-stg.okd.cern.ch` and hostnames under `apptest.cern.ch`. It's CERN network only,
can vanish without notice, and is for experiments, not for keeping a demo up.

## 2. Log in and check the quota

```bash
oc sso-login --server https://api.paas.okd.cern.ch
oc project ngt-demo
oc describe quota
```

By default a project has 10 pods and 1 CPU + 4 GiB of guaranteed `requests`, and `limits` above that are opportunistic.
This pod requests 400m CPU and 1.1 GiB of memory, with room left for the build pod, so **no ticket is needed**. If yours
is still short, it's the `requests` to lower, not the `limits`.

## 3. Create everything

From the root of your checkout:

```bash
oc apply -k airflow_automation/airflow_containers/openshift/
```

The pod can't start yet, because its image doesn't exist: `ImagePullBackOff` is expected at this point.

## 4. Build the image

```bash
oc start-build ngt-airflow --from-repo=. --follow
```

This sends your checkout's **committed** state (`HEAD`) to the cluster, which builds it there and pushes it to your
project's registry: a few minutes. Each finished build rolls out a new pod by itself, so this is also how you deploy a
change. When it's done:

```bash
oc rollout status deploy/ngt-airflow    # about a minute
oc get pods                             # 2/2 Running
```

## 5. Open the UI

```bash
oc get route ngt-airflow -o jsonpath='https://{.spec.host}{"\n"}'     # https://ngt-airflow-<project>.app.cern.ch
oc exec deploy/ngt-airflow -c airflow -- bash -c 'cat $AIRFLOW_HOME/simple_auth_manager_passwords.json.generated'
```

From the CERN network (or through a tunnel, e.g. `ssh -D` via LXPLUS), log in with one of the two generated accounts:
`viewer` to hand out, `admin` to keep to yourself. A new pod generates new passwords.

**Keep the Route as it is.** Without an `haproxy.router.openshift.io/ip_whitelist` annotation, CERN's routers only let in
the CERN network. `oc get route -o yaml` will show that annotation with CERN's address ranges: that's the platform adding
its default, so leave it. Setting it to `''` would put this login page on the Internet.

For a nicer name than the default, under `app.cern.ch`, `web.cern.ch` or `docs.cern.ch`:
`oc patch route ngt-airflow -p '{"spec":{"host":"ngt-demo.app.cern.ch"}}'`.

## 6. Play a scenario

```bash
oc exec deploy/ngt-airflow -c airflow -- airflow_demo.sh play scenario-player/scenarios/basic_ecal_pedestals.yaml
```

The rest is as on any cluster: [guided runs, and resetting between scenarios](../README.md#4-play-a-scenario).

## 7. Reset, stop, remove

```bash
oc rollout restart deploy/ngt-airflow     # reset: a new, empty pod
oc scale deploy/ngt-airflow --replicas=0  # stop, freeing the quota and losing all state; --replicas=1 starts it again
oc delete -k airflow_automation/airflow_containers/openshift/   # remove everything
```

## If something goes wrong

| What you see | Likely cause | What to do |
|---|---|---|
| `oc`: `error: EOF` | You're outside CERN | Use LXPLUS |
| Browser: `Secure Connection Failed`, `ERR_EMPTY_RESPONSE` | You're outside CERN: the Route drops the connection | Be on the CERN network or tunnel, e.g. `ssh -D` through LXPLUS |
| Build `Failed (OutOfMemoryKilled)` | A build's default limit is 3 GiB | Raise `spec.resources` in the `BuildConfig` |
| Build fails at `FROM` | Base image unreachable | Build elsewhere, push to GitLab or [Harbor](https://registry.cern.ch), set it as in the [overlay example](../README.md#adapting-it-to-your-cluster), and remove the trigger annotation |
| `ImagePullBackOff` on `postgres` | `registry.cern.ch` unreachable, or the path changed | `oc describe pod`; any `postgres:16` image will do |
| `exceeded quota` | Requests over the quota | `oc describe quota`; lower the `requests`, or scale down what else runs in the project |
| `unable to validate against any security context constraint`, `must be in the ranges` | Something asks for a specific UID | Shouldn't happen with this overlay; `oc get events` |
| "Application not available" | The router can't reach the pod | All containers ready? `oc get endpoints` non-empty? See the guide's FAQ |
| `HostAlreadyClaimed` | Someone else has that hostname | Choose another |
| `OOMKilled` | Over the memory limit | Raise the limit, or lower `AIRFLOW__CORE__PARALLELISM` |
| Restarting, liveness probe failed | A component died, and the probe restarts the container | `oc logs deploy/ngt-airflow -c airflow --previous`: each line starts with the component's name |
| `CrashLoopBackOff` | The same, repeatedly | After looking at the logs, delete the pod |

`oc logs deploy/ngt-airflow -c airflow` shows all four components, `oc exec -it deploy/ngt-airflow -c airflow -- bash` gets
you a shell. Older logs are at <https://logs.paas.cern.ch> (30 days, CERN network, owner and administrator group), and
resource use under Observe in the console.

## Putting CERN SSO in front (optional)

The platform team's [`cern-auth-proxy`](https://gitlab.cern.ch/paas-tools/okd4-deployment/cern-auth-proxy) chart puts a
CERN login in front of a Service.

**Beware: the chart defaults to Internet visibility.** In the console (+Add -> Helm Chart -> Cern Auth Proxy) turn
"Internet Visibility" **off**; in a values file, `route.public: false`:

```yaml
upstream:
  service: {name: ngt-airflow, port: 8080}
route:
  hostname: ngt-demo.app.cern.ch           # or anything free under app.cern.ch / web.cern.ch / docs.cern.ch
  public: false                            # CERN network only
authOptions:
  allowedRole: default-role
```

```bash
helm repo add paas-helm-charts https://gitlab.cern.ch/api/v4/projects/124069/packages/helm/master
helm install cern-auth-proxy -f values.yaml paas-helm-charts/cern-auth-proxy
oc delete route ngt-airflow
```

That last line is needed either way: a Route of ours on the same host gets the proxy's rejected until it's deleted, and
one on a different host bypasses the proxy. Remove the proxy later with `helm uninstall`, not by deleting its parts.

By default that lets in all CERN and eduGAIN users. To limit it, in the
[Application Portal](https://application-portal.web.cern.ch): `webframeworks-paas-<project>` -> Roles -> edit the default
role -> untick "applies to all authenticated users" -> add e-groups. Airflow's own login stays behind it.

## What's different from other clusters

Besides [what the deployment doesn't do anywhere](../README.md#what-this-deployment-doesnt-do):

- **The platform restarts or moves pods occasionally**, about monthly at most, and each time this demo comes back empty.
- **Its database goes against the guide's general advice**, which is against databases in a project in favour of DB On
  Demand. That's fine here, since nothing's kept, but it's no model for a lasting deployment.
- **The cluster keeps pod logs for 13 months**, for a demo whose logs nobody needs: about 7 lines and 1.6 KB per second while
  a scenario plays. Once it works, you can opt out, keeping `oc logs` and the console:
  `oc patch deploy/ngt-airflow -p '{"spec":{"template":{"metadata":{"labels":{"logging.okd.cern.ch/store-pod-logs":"false"}}}}}'`.
