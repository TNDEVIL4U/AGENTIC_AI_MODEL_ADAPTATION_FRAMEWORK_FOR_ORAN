# Kustomize

`deploy/kustomize/` is the alternative to the Helm chart for teams that do not run Helm. It
renders the same workload: the API (Deployment, Service, HPA, PDB), one worker Deployment, the
migration Job, a ServiceAccount and a default-deny NetworkPolicy. The optional objects
(Ingress, ExternalSecret, ServiceMonitor, PrometheusRule, extra worker pools) are yours to add in
an overlay; `deploy/helm/oran-adapt/files/prometheus-rules.yaml` can be used as-is as the rule
group.

```
kubectl apply -k deploy/kustomize/overlays/production
```

## Base

`base/` names no environment: images are bare names (`oran-adapt-api`, `oran-adapt-worker`,
`oran-adapt-migrator`), there is no namespace, and settings come from the ConfigMap
`oran-adapt-config` that an overlay fills (`configMapGenerator`, `behavior: merge`). Every pod
reads that ConfigMap and the Secret `oran-adapt-env` (`envFrom`). The Secret is not in the
tree; create it yourself, or with your secret operator, with keys named like the settings
(`DATABASE_URL`, `API_KEYS`, ...).

Probes, security context and shutdown are the chart's defaults ([helm.md](helm.md#probes-and-shutdown)).

## Overlays

| Overlay | Sets |
|---------|------|
| `overlays/dev` | namespace `oran-adapt-dev`, images tagged `dev` (loaded into a local cluster), debug logging, HPA minimum 1, PDB `minAvailable: 0` |
| `overlays/production` | namespace `oran-adapt`, images from your registry pinned by digest, the MLflow registry and KServe serving adapters, private metrics, an extra NetworkPolicy admitting the ingress controller to the API |

The registry host, MLflow URL and digests in `overlays/production` are placeholders. Copy the
overlay and replace them.

## Migrations without hooks

Kustomize has no hooks, so ordering comes from the pods themselves:

1. The Job `oran-adapt-migrate` runs `oran-adapt db upgrade`.
2. Every API and worker pod starts with the `wait-for-schema` init container
   (`oran-adapt db wait`), so the pods start as soon as the Job has migrated the schema, and
   not before.

A Job's pod template is immutable, so each release gives the Job a new name. The production
overlay does this with a patch (`oran-adapt-migrate-0-1-0`); bump it with the image digests. Or
delete the finished Job before applying. Migrations are expand-only
([migrations.md](migrations.md)), so the previous release keeps running while the Job runs, and
rolling back is re-applying the previous overlay.

## Checks

The CI `packaging` job runs `kubectl kustomize` on every overlay and validates the output with
`kubeconform`. Locally, `bash scripts/verify.sh 11` checks that the tree is complete, that the
base holds no environment-specific literal, and that every setting an overlay passes is a key
the application reads. Nothing here has been applied to a cluster from the development laptop.
