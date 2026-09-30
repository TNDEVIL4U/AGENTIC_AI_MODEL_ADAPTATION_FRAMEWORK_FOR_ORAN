# Limitations

This page lists what the framework does not do, the limits of what it does, and what has not been
run anywhere except against local doubles. Read it before promising any of these things to a
site. The open decisions behind several of these limits are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md),
and the chosen defaults in [adr/README.md](adr/README.md).

## 1. Not verified outside the development machine

The development laptop has no Docker, no Kubernetes and no cloud credentials. Everything below
has adapters, tests and docs, but it was tested only against local emulators, wire-format
doubles or not at all. Treat each item as **unverified** until a site has run it.

| Area | What ran locally | Not run |
|---|---|---|
| Containers | nothing | `docker compose up`, image builds and SBOMs, the compose walkthrough (`deploy/compose/walkthrough.yml`) |
| Kubernetes packaging | chart and manifest structure tests | `helm lint/template`, `kustomize build`, kubeconform, a kind install/upgrade/rollback |
| Cloud registries and endpoints | API emulators (`tests/unit/registry_emulators.py` and the deployment emulators) | AWS SageMaker/S3, Google Vertex AI/GCS |
| Serving systems | KServe/Seldon/k8s API emulator; Triton and BentoML stubs; git + a controller emulator for GitOps | a real cluster, KServe, Seldon, Triton, BentoML, Argo CD, Flux |
| Job queues | client and app doubles | Celery with a broker, RQ with Redis, Kubernetes Jobs |
| CDC and brokers | protocol doubles | Kafka/Debezium, SQS, SNS, Pub/Sub, NATS |
| Notifications | local receivers | Slack, PagerDuty, a real SMTP relay |
| Identity and secrets | local IdP, gateway and Vault doubles | a real OIDC provider, Envoy/ingress mTLS, Vault/OpenBao |
| Policy | an OPA Data API double (httpx mock transport) | an OPA server running Rego |
| LLM providers | local wire-format doubles | the live Anthropic, Gemini and OpenAI-compatible services |
| Model types | test doubles for LightGBM, CatBoost and Keras; the `onnx` reference evaluator | the real LightGBM, CatBoost, Keras/TensorFlow and onnxruntime |
| Sandbox | the `subprocess` backend | the `docker` sandbox backend |
| Observability | metrics and trace tests in-process | OTLP export, promtool, the Grafana import, the PodMonitor |
| Tests | the smoke and gate tiers | the testcontainers tier (PostgreSQL, Kafka) and the heavy scenarios, which run in CI only |

The in-process walkthrough (`python scripts/walkthrough.py`) is verified. It drives the real
API, pipeline, filesystem registry and `registry-alias` deployment from an Alertmanager payload
to a canary.

## 2. Scope

- **The framework adapts models; it does not detect drift.** Drift arrives as a DriftEvent,
  directly or through a mapper, from a monitoring system the site already runs.
- **Mappers are declarative.** A mapping file selects, renames, splits, translates and joins
  fields ([integration-guide.md](integration-guide.md#1-monitoring-to-driftevent-mappers)). It
  cannot compute, loop over nested lists beyond `records` and `[feature_table]`, or call out.
  A payload that needs more needs a small translator in front of the API.
- **Traffic splitting needs an adapter that can split.** Only `registry-alias`, `kserve` and
  `webhook` have the `traffic_split` feature. With the others, `canary` and `ab` are refused by
  config lint and at startup, and delivery is `blue_green`, `manual` or `shadow`.
  - `registry-alias` records the split in a canary alias and a tag. The serving layer must read
    them to honour the split.
- **Model types.** Continued training exists only where the library supports it. ONNX models
  are inference-only: the framework has no engine that can retrain one
  ([adapters/model_type.md](adapters/model_type.md)).

## 3. Operational limits

- **Rate limits are per replica.** With N API replicas a caller gets up to N times the limit.
  Enforce a global limit in the gateway or ingress ([security.md](security.md)).
- **DNS rebinding is not fully covered in-process.** The outbound policy checks the address at
  request time, and a DNS server can answer differently when the connection resolves the name
  again. Close this with an egress NetworkPolicy, firewall or egress proxy.
- **Unknown environment variables are ignored.** A retired or misspelt key in the environment
  has no effect and raises no warning. Use `oran-adapt config effective` to see what is in
  force. A configuration file is stricter: `oran-adapt config lint` rejects unknown keys in it.
- **SQLite is for development only.** Production (`ENVIRONMENT=production`) requires an explicit
  `DATABASE_URL`. Use PostgreSQL: the job queue's row locking and concurrent workers are only
  meaningful there.
- **A failed restore is reported, not retried.** If a rollout fails and restoring the previous
  version cannot be read back either, the error says `restored: false`, and an operator acts
  ([runbooks/OranAdaptDeliveryFailures.md](runbooks/OranAdaptDeliveryFailures.md)).
- **Migrations are expand-only.** Columns and tables that are no longer used are left in place
  until a later, separately planned contract migration.
- **The LLM is optional and fenced.** It is off by default, and a failure never fails a job.
  Its circuit breaker is kept per provider **per process**, so each replica and worker opens
  its own ([adapters/llm.md](adapters/llm.md)).
- **Triton needs a shared model repository.** The framework stages artifacts into a directory
  that the Triton server also mounts.

## 4. Where this is tracked

- [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md): open decisions, each with the default chosen.
- [assumption-inventory.md](assumption-inventory.md): assumptions and the phase that closed each.
- The `PHASE*_REPORT.md` files: the "Unverified locally" section of each phase.
