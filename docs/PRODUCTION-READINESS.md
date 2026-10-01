# Production readiness

This page covers what has been proven, what a site must prove itself before going live, and
the checklist to follow. It is the last document of the hardening program. The program's
evidence is in the phase reports (`docs/PHASE*_REPORT.md`) and the two audits:

- [AUDIT-FINDINGS.md](AUDIT-FINDINGS.md): each of the ten findings, with its module, config key
  and passing test;
- [AUDIT-HARDCODING.md](AUDIT-HARDCODING.md): every baseline literal, with zero remaining.

`scripts/verify.sh all` runs every phase gate and both audit scans.

## 1. Status

The framework is **ready for a staged production trial**. It is not yet ready for an
unsupervised rollout, because the integrations below have never run against real services.

| Area | Verified locally (the gate runs it) | A site must verify |
|---|---|---|
| Core pipeline | drift event → analysis → decision → retrain → gate → delivery, in-process, on SQLite and the filesystem registry; the clone-to-canary walkthrough | the same on PostgreSQL with its chosen registry |
| Registry and deployment | conformance suites for every adapter, against emulators; deploy → read back → roll back | the chosen registry and serving system (MLflow server, KServe, SageMaker, Vertex AI, Seldon, Triton, BentoML, GitOps) |
| Execution | leases, deadlines that kill, cancel, retries, poison quarantine and drain, on the database queue | Celery, RQ or Kubernetes Jobs, if chosen |
| Validation and delivery | superiority gate with guardrails; shadow, canary, A/B, blue/green and manual strategies; automatic rollback | the rollout metrics source (Prometheus or the serving layer's posts) |
| Security | deny-by-default authorization over every route, OIDC against a local issuer, SSRF suite, secret-leak scan | the real identity provider, gateway or mTLS, Vault, and the CVE scans (CI) |
| LLM | off by default; the pipeline runs with egress blocked; the fallbacks and budgets | the live provider, if enabled |
| Packaging | static checks of the Dockerfile, compose file, chart and kustomize tree | image builds, `docker compose up`, `helm lint/template`, a cluster install, upgrade and rollback |
| Observability | metrics, traces and logs in-process; alert rules and one runbook per alert | OTLP export, the Prometheus rules loaded, dashboards imported |

[LIMITATIONS.md](LIMITATIONS.md) has the full list, and each phase report has an "Unverified
locally" section.

## 2. Prerequisites

- PostgreSQL 16 or later. SQLite is for development only.
- A model registry. MLflow is the default, with a PostgreSQL backend and artifact storage.
- A serving system with a deployment adapter. Use `webhook` for GitOps or anything
  unlisted.
- For canary or A/B delivery:
  - a deployment adapter that can split traffic (`registry-alias`, `kserve` or `webhook`);
  - a rollout metrics source.
- An identity provider (OIDC), a trusted gateway or mTLS. API keys are for development and
  service accounts.
- A secrets source: mounted files, Vault or OpenBao, or the environment.
- Kubernetes with the Helm chart or the kustomize tree, or another orchestrator that runs the
  three images: api, worker and migrator.

## 3. Keys that production requires

With `ENVIRONMENT=production`, startup refuses in these cases:

- `DATABASE_URL` or `ARTIFACT_WORKDIR` is left at its default.
- A production key of a selected adapter is left at its default, for example
  `MLFLOW_TRACKING_URI` with the `mlflow` registry.
- An adapter marked development-only is selected.

The following keys have no default and must be set whenever their feature is chosen. Hardening
Phase 15 removed their defaults.

| Feature | Keys |
|---|---|
| `LLM_ENABLED=true` with `anthropic` / `gemini` / `openai-compatible` | the provider's API key and `ANTHROPIC_MODEL` / `GEMINI_MODEL` / `LLM_OPENAI_MODEL` |
| `SANDBOX_BACKEND=docker` | `SANDBOX_DOCKER_IMAGE`, pinned by digest (`name@sha256:...`) |
| `CDC_MODE=kafka` | `KAFKA_BOOTSTRAP_SERVERS`, `CDC_KAFKA_TOPIC`, `CDC_CONSUMER_GROUP` |

Each adapter's keys are listed in [capability-matrix.md](capability-matrix.md). `oran-adapt
config lint FILE` checks a configuration file offline, and `oran-adapt config effective` shows
what is in force and where each value came from.

## 4. Go-live checklist

**Configuration**

1. Start from the closest file in `config/examples/`. Run `oran-adapt config lint` on the
   result.
2. Set `ENVIRONMENT=production`, and set every key from section 3 that applies.
3. Set `GATE_POLICY` and `DELIVERY_POLICY`, or point them at reviewed policy files. Record the
   policy version.
4. Choose `DELIVERY_STRATEGY`. The default is `shadow`, the safest. Go to canary only once the
   metrics source has been checked.

**Deployment**

5. Build the images, or pull them by digest, and check their SBOMs and CVE scan
   ([operations/images.md](operations/images.md)).
6. Run the migrator Job before the new API and workers
   ([operations/migrations.md](operations/migrations.md)).
7. Install with Helm or kustomize ([operations/helm.md](operations/helm.md),
   [operations/kustomize.md](operations/kustomize.md)). Check the probes, the PodDisruptionBudget,
   the NetworkPolicy and the egress rules.

**Security**

8. Turn on authentication with the site's identity provider. Map its roles in
   `POLICY_ROLES` or OPA ([security.md](security.md)).
9. Set `OUTBOUND_ALLOWLIST` to the hosts the framework must reach, and nothing else.

**Observability**

10. Scrape the API and worker metrics, and load the alert rules. Check that every alert opens
    its runbook ([operations/observability.md](operations/observability.md), `runbooks/`).

**Trial and rollback**

11. Run the conformance suite of each chosen adapter against the real service. The authoring
    guide ([adapter-authoring.md](adapter-authoring.md)) shows how.
12. Rehearse with one model and one drift source:
    - send a mapped event;
    - watch the job, the gate decision and the rollout;
    - force a canary breach and confirm the automatic rollback reads back the stable version.
13. Rehearse an upgrade and a rollback of the framework itself (Helm `upgrade` / `rollback`).
    The migrations are expand-only, so the previous release still runs on the new schema.
14. Keep the LLM off until the deterministic path has run for a while. Then enable it with a
    token and cost budget.

## 5. Who owns what after go-live

| Concern | Where |
|---|---|
| An alert fired | its runbook in `docs/runbooks/` |
| A decision or promotion to explain | the job record, the gate decision record, and the append-only `audit_log` table ([security.md](security.md)) |
| A new stack or service | [integration-guide.md](integration-guide.md) and [adapter-authoring.md](adapter-authoring.md) |
| An open question or default to revisit | [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) and [adr/README.md](adr/README.md) |
