# Hardening Phase 11 report: packaging and deployment

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled. What ran for real
and what used doubles:
- **Ran for real:**
  - the liveness file, `oran-adapt worker health`, the health-window validator and the worker
    loop's beats;
  - `schema_status` and `db wait` against real SQLite databases (empty, migrated, and stamped
    with an unknown revision), and the `db status`, `db wait` and `worker health` CLI commands;
  - static checks of every packaging file: Dockerfile stages and pins, compose ordering and pins,
    every Helm example validated against `values.schema.json`, every `.Values` path a template
    reads, the required Kubernetes kinds, the environment-literal scan, settings keys, alerts
    against runbooks, the kustomize tree, and the expand-only AST check of every migration.
- **Doubles:** none. The chart and overlays are parsed as text and YAML, not rendered.

**Unverified locally** (the laptop has no Docker, Helm, kubectl or cluster, and none may be
installed):
- `docker compose up` from a fresh clone and `scripts/ci/compose_smoke.sh` (CI job
  `phase-0-baseline`);
- the image builds of the `api`, `worker` and `migrator` targets, their SBOMs and health checks
  (CI job `build-images`);
- `helm lint` and `helm template` for every example values file, `kubectl kustomize` for every
  overlay, and kubeconform on the output (CI job `packaging`);
- the kind install, upgrade and rollback (`scripts/ci/kind_e2e.sh`, the scheduled `k8s-e2e`
  workflow);
- the digests themselves: they were taken from the registries' published indexes and have not
  been pulled from this machine.

## 1. Findings closed

| Finding | What changed |
|---|---|
| #7 "never packaged" (assumption 9) | One Dockerfile with three targets (`api`, `worker`, `migrator`), non-root, base pinned by digest, a health check per target, SBOMs in CI. A Helm chart and a kustomize alternative. Compose migrates once, before anything reads the schema |
| The API migrated on start | A one-shot migrator (compose `migrate`, a Helm `pre-install,pre-upgrade` hook Job, a kustomize Job). Every pod waits in a `wait-for-schema` init container (`oran-adapt db wait`) |
| Workers had no liveness signal | `core/liveness.py`: the worker touches `WORKER_HEALTH_FILE` every loop turn and heartbeat; `oran-adapt worker health` is the probe; the settings refuse a window shorter than two beats |
| Rollback and migrations were coupled | Migrations are expand-only (checked by AST on every migration); `db status` reports `ahead` for a newer schema and `db wait` accepts it, so rollback never touches the database |
| Hardcoding D1, D2, D3 | Base pinned by digest; the torch index is the build arg `TORCH_INDEX_URL`; UID, port, probes and grace periods are chart and overlay values |
| Found in this phase: the expand-only check never saw a NOT NULL `add_column` | It read the table name as the column. It now reads the `Column(...)` argument, and its self-test covers both cases |
| Found in this phase: three CI `run:` lines had lost their line continuations | Restored in `ci.yml` (`build-images`, `packaging`) |

## 2. Ports and adapters

No new port. The chart maps the existing ports to deployment objects: one ConfigMap per port
whose adapter a release chooses (`adapters.<port>.env`), and one worker Deployment per queue
class (`workers[].classes` → `JOB_WORKER_CLASSES`).

## 3. Configuration keys

| Key | Default | Meaning |
|---|---|---|
| `WORKER_HEALTH_FILE` | unset (no file); `/tmp/oran-adapt/worker.alive` in the images | The liveness file the worker touches |
| `WORKER_HEALTH_MAX_AGE_S` | 120 | Older than this means stuck; must exceed 2 × max(`JOB_HEARTBEAT_S`, `JOB_POLL_INTERVAL_S`) |
| `MIGRATION_WAIT_TIMEOUT_S` | 600 | How long `db wait` waits |
| `MIGRATION_WAIT_INTERVAL_S` | 2 | How often it looks |
| build arg `PYTHON_IMAGE` | `python:3.13.7-slim-bookworm@sha256:...` | Base image, by digest |
| build arg `TORCH_INDEX_URL` | the PyTorch CPU index | torch wheel index |
| Helm values | `values.yaml`, schema-validated | [docs/operations/helm.md](operations/helm.md) |

## 4. Acceptance criteria

| Criterion (spec) | Evidence | Status |
|---|---|---|
| Dockerfiles for api/worker/migrator: non-root, pinned digests, SBOM, healthcheck | `test_the_dockerfile_builds_...`, `test_every_other_image_base_is_pinned_by_digest`; CI `build-images` | static checks passed; build and SBOM unverified locally |
| Compose clean from a fresh clone | `test_compose_*`; `.env` optional; CI `phase-0-baseline` | static checks passed; `up` unverified locally |
| Helm chart: API + HPA + PDB, workers per queue class incl. GPU, migration Job with hooks, ConfigMaps per adapter, ExternalSecrets, Ingress + TLS, NetworkPolicy, ServiceMonitor, PrometheusRule | `test_the_chart_has_every_required_object`, schema and values-path tests | passed (text level); lint and render in CI |
| Kustomize alternative | `test_the_kustomize_tree_is_complete` | passed; build in CI |
| Probes, graceful shutdown | liveness, health-window, worker-loop and CLI tests; preStop + grace values | passed |
| Expand/contract migrations | `test_every_migration_upgrade_only_expands_the_schema`, schema-status and db-wait tests | passed |
| helm lint and template pass for every example values file | CI `packaging` | unverified locally |
| No manifest contains an environment-specific literal | `test_no_manifest_holds_an_environment_specific_literal`, `test_every_setting_the_manifests_pass_...` | passed |
| kind/k3d install + e2e + upgrade + rollback | `scripts/ci/kind_e2e.sh`, weekly `k8s-e2e` | unverified locally (by the spec, pushed to the schedule) |

## 5. Hardcoding

D1, D2 and D3 are closed (section 1); D is 5 open after this phase. What is still a literal, and
why:

| Where | Value | Why it is not a key |
|---|---|---|
| `Dockerfile` | UID/GID 10001, `/tmp/oran-adapt/...` paths | the image's identity and layout; Helm and kustomize set `runAsUser` and mount `/tmp` |
| `deploy/helm/.../values.yaml` | defaults (replicas, resources, probe timings, grace periods) | chart defaults, every one a value |
| `deploy/kustomize/base/*.yaml` | the same defaults | a base is patched by overlays |
| `deploy/kustomize/overlays/*`, `deploy/helm/.../examples/*` | namespaces, `example.com` hosts, zero digests | examples of an environment, marked as placeholders |
| `docker-compose.yml` | in-network hosts, DB user and names (D4-D6) | the development stack's own topology |

## 6. Assumptions and defaults

- The API and workers run the same code version as the migrator of their release, or older.
  A newer pod never runs against an older schema: its init container waits.
- The default ServiceAccount of the namespace exists (the migration Job runs under it with no
  token, because the release's ServiceAccount does not exist at pre-install).
- The external-secrets operator is installed when `secrets.externalSecret.enabled`.
- The GPU pool is scheduling only; the in-tree trainers are CPU only.
- `metrics.path` is served on the API port; Prometheus reaches it through `metricsFrom`.

## 7. Unverified locally

Everything in the header's list. In addition: `values-airgapped.yaml` assumes an in-cluster
mirror and MLflow whose names are placeholders, and the build still needs PyPI (or a mirror
reachable as the default index).

## 8. Gate

`bash scripts/verify.sh 11`: **PASS in 178 s** (budget 300 s).

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean (mypy: 174 files) |
| 2 import boundary | 5 s | 2 passed |
| 3 no-gaps lint | 17 s | clean |
| 4 scoped tests (core.liveness, db.migrate; 5 files) | 18 s | 160 passed in 102 s |
| 4 smoke tier (files not run above) | 129 s | 206 passed in 40 s |
| 5 acceptance (`scripts/acceptance/phase11.py`) | 178 s | 7/7 passed (recorded results) |

After the gate, the Dockerfile's torch index became the build arg `TORCH_INDEX_URL`. The two
Dockerfile tests were re-run: 2 passed.
