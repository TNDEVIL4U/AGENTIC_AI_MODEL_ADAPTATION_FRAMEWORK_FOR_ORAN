# Helm chart

`deploy/helm/oran-adapt/`. The chart names no environment: registries, hosts, namespaces,
secret stores and node pools come from your values file. `values.schema.json` rejects unknown
keys, so a typo fails `helm lint`/`install` rather than being ignored.

```
helm install oran deploy/helm/oran-adapt -n oran-adapt --create-namespace \
  --values my-values.yaml --wait
```

Start from an example in `deploy/helm/oran-adapt/examples/`:

| File | For |
|------|-----|
| `values-dev.yaml` | One replica each, no autoscaling, an existing Secret |
| `values-production.yaml` | Digests, Ingress + TLS, ExternalSecret, ServiceMonitor and PrometheusRule, NetworkPolicy peers and egress, MLflow registry + KServe serving adapters |
| `values-gpu.yaml` | A second worker pool on GPU nodes (see [GPU worker pools](#gpu-worker-pools)) |
| `values-airgapped.yaml` | Images from an in-cluster mirror, egress limited to the cluster and DNS, LLM and MLflow telemetry off, registry-alias serving |

Hosts, registries and digests in the examples are placeholders (`example.com`, all-zero
digests). Replace them with yours.

## What it renders

| Object | Condition | Notes |
|--------|-----------|-------|
| ConfigMap `<release>-config` | always | `config.env`: non-secret settings shared by every pod |
| ConfigMap `<release>-adapter-<port>` | one per `adapters.<port>` | the adapter selector and its keys, e.g. `registry: {env: {REGISTRY_BACKEND: mlflow, MLFLOW_TRACKING_URI: ...}}` ([docs/adapters/](../adapters/)) |
| ExternalSecret `<release>-env` | `secrets.externalSecret.enabled` | syncs the pods' Secret from your store ([below](#secrets)) |
| ServiceAccount | `serviceAccount.create` | token not mounted |
| Job `<release>-migrate` | `migrations.enabled` | hook ([below](#hooks)) |
| Deployment + Service `<release>-api` | always | probes on `/api/v1/health` (startup, liveness) and `/api/v1/ready` (readiness) |
| HorizontalPodAutoscaler | `api.autoscaling.enabled` | CPU target |
| PodDisruptionBudget | `api.pdb.enabled` | `minAvailable` |
| Ingress | `api.ingress.enabled` | `hosts`, `tls`, `className` from values |
| Deployment `<release>-worker-<name>` | one per `workers[]` entry | `JOB_WORKER_CLASSES` = the entry's `classes` |
| NetworkPolicy | `networkPolicy.enabled` | default deny ingress for the release; the API admits `apiIngressFrom` on its port and `metricsFrom` for scraping; `egress` rules if set |
| ServiceMonitor | `metrics.serviceMonitor.enabled` | scrapes `metrics.path` |
| PrometheusRule | `metrics.prometheusRule.enabled` | `files/prometheus-rules.yaml`; each alert's `runbook` annotation names a file in [docs/runbooks/](../runbooks/) |

Every pod reads, in order: the common ConfigMap, each adapter ConfigMap, then the Secret
(`envFrom`). A key set in two places takes the later value.

## Hooks

Install and upgrade run in this order:

1. **ExternalSecret** (`pre-install,pre-upgrade`, weight -10), when enabled. The Secret it syncs
   is `creationPolicy: Orphan`, so recreating the ExternalSecret on each upgrade never deletes the
   Secret under running pods.
2. **Migration Job** (`pre-install,pre-upgrade`, weight 0): `oran-adapt db upgrade` with the
   migrator image. Its settings are inlined from values, because the release's ConfigMaps do not
   exist yet at pre-install. It uses the namespace's default ServiceAccount with no token for
   the same reason. If the Secret is not synced yet, the pod waits in `ContainerCreating`
   until it is. A failed Job fails the install or upgrade, and nothing rolls.
3. **The release's objects**. Every API and worker pod starts with a `wait-for-schema` init
   container (`oran-adapt db wait`, up to `migrations.waitTimeoutS`), so no pod serves on a
   schema older than its code, even if the hooks are skipped (`--no-hooks`).

Migrations are expand-only ([migrations.md](migrations.md)): while the Job runs, the previous
release's pods keep working. `helm rollback` rolls the pods back and leaves the schema as it is.
The Job does not run on rollback, and it does not need to.

## Secrets

The chart never holds a secret value. Choose one:

- `secrets.existingSecret: <name>`: a Secret you create, with keys named like the settings
  (`DATABASE_URL`, `API_KEYS`, `MLFLOW_TRACKING_TOKEN`, provider keys).
- `secrets.externalSecret`: `secretStoreRef` names your (Cluster)SecretStore, and `data` maps
  each setting to a remote key (`DATABASE_URL: {key: oran/db, property: url}`). Requires the
  external-secrets operator.

On `helm uninstall` the orphaned Secret stays. Delete it by hand with
`kubectl delete secret <fullname>-env` (`<fullname>` is `<release>-oran-adapt`, or the release
name when it already contains `oran-adapt`) once nothing uses it. Hook objects are not
removed by uninstall either: delete the ExternalSecret and the finished migration Job the same way.

## Probes and shutdown

| Pod | Liveness | Readiness | On SIGTERM |
|-----|----------|-----------|------------|
| API | `GET /api/v1/health` | `GET /api/v1/ready` (database, model registry and serving system reachable) | `preStop` sleeps `api.preStopSleepS` so endpoints drop the pod; uvicorn then gives in-flight requests `api.gracefulShutdownS`. `terminationGracePeriodS` must exceed the sum |
| Worker | exec `oran-adapt worker health`: the liveness file (`worker.healthFile`) was touched within `WORKER_HEALTH_MAX_AGE_S` | none (workers take no traffic) | stops claiming; the running job gets `JOB_DRAIN_TIMEOUT_S`, then is requeued. `worker.terminationGracePeriodS` must exceed it |

The worker touches the liveness file on every loop turn and every job heartbeat.
`WORKER_HEALTH_MAX_AGE_S` must exceed twice the slower of `JOB_HEARTBEAT_S` and
`JOB_POLL_INTERVAL_S`; the settings refuse to load otherwise.

## Worker pools

Each `workers[]` entry is a Deployment that claims only its `classes`. Route jobs to classes
with `JOB_CLASS_BY_FRAMEWORK` (in `config.env`); unrouted jobs go to `default`, so keep one pool
that claims `default` ([docs/adapters/job_queue.md](../adapters/job_queue.md)). Each pool has
its own `replicas`, `resources`, `nodeSelector`, `tolerations`, `affinity` and extra `env`.

### GPU worker pools

`values-gpu.yaml` adds a `gpu` pool that requests `nvidia.com/gpu` and schedules onto GPU nodes,
and routes `torch` jobs to it. **Limitation:** the in-tree adaptation strategies train on the
CPU; the framework has no device setting. The pool gives GPU nodes to model-type plugins that
choose the device themselves. For the in-tree strategies, a GPU pool buys nothing.

## Checks

Run in the CI `packaging` job, not on the development laptop:

- `helm lint` on the chart, then `helm lint` and `helm template` for every example values file;
- `kubeconform -strict` on everything rendered;
- the scheduled `k8s-e2e` workflow: install on kind, upgrade, rollback (`scripts/ci/kind_e2e.sh`).

Local, in `bash scripts/verify.sh 11`: every example validates against `values.schema.json`,
every `.Values` path a template reads is declared, the required objects exist, no manifest holds
an environment-specific literal, every setting a manifest passes is a key the application reads,
and every alert has a runbook.
