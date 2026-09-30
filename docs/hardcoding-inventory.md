# Hardcoding inventory (Hardening Phase 0 baseline)

This is the checklist that Hardening Phase 1 burns down and Phase 15 audits against. Line numbers
are from commit `2893513`. The proposed keys follow a dotted layout (`section.key`) for the future
layered config. Today's flat env names appear where a key already exists.

How the list was built: a grep of `src/oran_adapt` for module-level constants, URLs, hosts and
ports, inline numeric literals in logic and signatures, duplicated defaults, framework and alias
strings, and table and column names, followed by reading each hit. Pure maths (`** 0.5` for
RMSE, `0.5 *` in TVD), zero or empty initialisers in result dataclasses, and DB column widths are
not listed. Column widths are schema definitions that change through migrations, not config.

Status legend: **LIT** = literal at the use site; **DUP** = a second default that shadows a
`Settings` field and can drift from it; **CFG** = already a `Settings` field whose default is
environment- or vendor-specific and needs review; **INFRA** = deployment files.

## A. Literals at use sites in `src/` (must become config)

| # | file:line | Value | Controls | Status | Proposed key |
|---|---|---|---|---|---|
| A1 | `llm/client.py:36` | `max_tokens=2048` | LLM response cap (Anthropic) | LIT | `llm.max_output_tokens` |
| A2 | `llm/client.py:27` | `max_retries=0` | LLM SDK retries | LIT | `llm.retry.max_attempts` |
| A3 | `orchestrator/jobs.py:102` | `_KILL_GRACE_S = 5.0` | grace between terminate and kill of a job worker | LIT | `jobs.kill_grace_s` |
| A4 | `sandbox/runner.py:295` | `--pids-limit 128` | sandbox process cap | LIT | `sandbox.docker.pids_limit` |
| A5 | `sandbox/runner.py:297` | `--cpus 1` | sandbox CPU cap | LIT | `sandbox.docker.cpus` |
| A6 | `sandbox/runner.py:301` | `size=64m` | sandbox tmpfs size | LIT | `sandbox.docker.tmpfs_mb` |
| A7 | `sandbox/runner.py:321` | `timeout=60` | `docker rm` cleanup timeout | LIT | `sandbox.docker.cleanup_timeout_s` |
| A8 | `sandbox/runner.py:74` | `_MANIFEST_MAX_BYTES = 4096` | max size of sandbox result manifest | LIT | `sandbox.manifest_max_bytes` |
| A9 | `cdc/materialize.py:21` | `_MAX_TX_IDS = 1000` | CDC transaction ids kept per version | LIT | `cdc.max_tx_ids_per_version` |
| A10 | `cdc/sources.py:40` | `table="kpi_sample"` | CDC polling source table | LIT | `cdc.sources[].table` |
| A11 | `cdc/events.py:23` | `"kpi_sample/1"` | CDC payload schema version tied to one table | LIT | `cdc.sources[].schema_ref` |
| A12 | `cdc/consumer.py:64` | `idle_sleep_s=1.0` | CDC idle poll interval | LIT | `cdc.idle_poll_s` |
| A13 | `decision/report.py:33` | `_MEMORY_COPIES = 3` | memory estimate multiplier in decision report | LIT | `decision.resource_estimate.memory_copies` |
| A14 | `decision/engine.py:99` | `confidence=0.5` | default decision confidence without evidence | LIT | `decision.default_confidence` |
| A15 | `analysis/retrieval.py:169` | `.limit(10)` | performance records pulled per job | LIT | `analysis.performance_history_limit` |
| A16 | `datastore/current_data.py:197` | `limit=50` | page size for `GET /current-data` | LIT | `api.pagination.default_limit` |
| A17 | `core/metrics.py:14` | `_EVAL_BUCKETS` | Prometheus histogram buckets | LIT | `metrics.duration_buckets_s` |
| A18 | `registry/promotion.py:48` | `"artifact.sha256"` | registry tag name for checksum | LIT | `registry.tags.checksum` |
| A19 | `registry/promotion.py:49` | `"oran.status"` | registry tag name for lifecycle status | LIT | `registry.tags.status` |
| A20 | `core/correlation.py:15` | `"X-Correlation-ID"` | correlation header name | LIT | `api.correlation_header` |
| A21 | `api/security.py:53` | `"X-API-Key"` | API-key header name | LIT | `auth.api_key.header` |
| A22 | `validation/engine.py:69` | `next(iter(current_metrics))` | primary metric = first dict key (implicit) | LIT | `policy.gate.primary_metric` |
| A23 | `adaptation/inspector.py:11-12`, `adaptation/loaders.py:10-11`, `adaptation/engines.py:23`, `validation/evaluate.py:29`, `analysis/summary.py:18-19`, `registry/client.py:199` | `{"sklearn","xgboost"}`, `{"torch","pytorch"}` | framework → code path dispatch (repeated in 6 files) | LIT | handler registry (Hardening Phase 8); no key |
| A24 | `analysis/retrieval.py:64`, `adaptation/data.py` (12 occurrences in `src/`) | `"observed_at"` | time column name injected into every row | LIT | `datasets.<id>.time_column` |

## B. Duplicated defaults (shadow a Settings field)

| # | file:line | Value | Shadows | Action |
|---|---|---|---|---|
| B1 | `core/integrity.py:15` | `DEFAULT_ARTIFACT_MAX_BYTES = 2 GiB` | `artifact_max_bytes` | remove, require caller to pass |
| B2 | `core/integrity.py:12` | `_CHUNK = 1 MiB` | – (hash read chunk) | `artifacts.hash_chunk_bytes` |
| B3 | `adaptation/torch_engine.py:84-85` | `epochs=5, lr=1e-2` | `torch_fine_tune_epochs`, `torch_learning_rate` | remove defaults |
| B4 | `adaptation/torch_engine.py:119-120` | `epochs=300, lr=1e-2` | `torch_full_retrain_epochs`, `torch_learning_rate` | remove defaults |
| B5 | `analysis/reuse.py:45` | `min_psi_rows=30` | `analysis_min_psi_rows` | remove default |
| B6 | `registry/onboarding.py:83,176` | `live_alias="live"` | `live_alias` | remove default |
| B7 | `adaptation/llm_adapter.py:73-74` | `sandbox_backend="subprocess"`, `sandbox_docker_image=""` | `sandbox_backend`, `sandbox_docker_image` | remove defaults |

## C. Settings defaults that are environment- or vendor-specific (`core/config.py`)

These are already configurable and are not literals at a use site. They are listed because
Hardening Phase 1 has to decide, for each one, whether it stays a schema default or becomes
**required**.

| # | Key (env) | Default | Concern | Proposed |
|---|---|---|---|---|
| C1 | `DATABASE_URL` | `sqlite:///./data/oran_adapt.db` | path default | keep for dev profile only; required in prod profile |
| C2 | `MLFLOW_TRACKING_URI` | `sqlite:///./data/mlflow.db` | assumes MLflow | move under `registry.mlflow.*` (adapter config) |
| C3 | `ARTIFACT_WORKDIR` | `./data/artifacts` | path | `artifacts.workdir` |
| C4 | `ANTHROPIC_MODEL` | `claude-sonnet-5` | model id | `llm.providers.anthropic.model` (required when enabled) |
| C5 | `GEMINI_MODEL` | `gemini-3.6-flash` | model id | `llm.providers.gemini.model` (required when enabled) |
| C6 | `SANDBOX_DOCKER_IMAGE` | `oran-adapt-sandbox:latest` | mutable `:latest` tag | required, pinned by digest |
| C7 | `LIVE_ALIAS` / `CANDIDATE_ALIAS` | `live` / `candidate` | MLflow alias semantics | `deployment.targets[].alias` (registry-alias adapter only) |
| C8 | `DECISION_SUPPORTED_FRAMEWORKS` | `sklearn, xgboost, torch, pytorch` | vendor list in config | closed in Hardening Phase 1 (derived default); **since Hardening Phase 8** the default `[]` means every framework an installed model type plugin can adapt; set it only to narrow the list |
| C9 | `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | host:port | required when `cdc.mode=kafka` |
| C10 | `CDC_KAFKA_TOPIC` | `oran.public.kpi_sample` | table-bound topic | `cdc.sources[].topic` |
| C11 | `CDC_CONSUMER_GROUP` | `oran-adapt-cdc` | name | `cdc.consumer_group` |
| C12 | `MLFLOW_SKOPS_TRUSTED_TYPES` | two sklearn types | vendor-specific | `handlers.sklearn.trusted_types` |
| C13 | `VALIDATION_ACCURACY_TOLERANCE` / `VALIDATION_RMSE_TOLERANCE_RATIO` | `0.02` / `0.05` | tolerance lets a worse model pass | **closed in Hardening Phase 7**: both keys removed; the versioned `GATE_POLICY` (margin, confidence interval, guardrails) replaces them |
| C14 | all threshold / budget keys (`ANALYSIS_*`, `REUSE_*`, `DECISION_*`, `VALIDATION_*`, `TORCH_*`, `JOB_*`) | see `.env.example` | fine as schema defaults | validation and delivery thresholds moved to policy files in Hardening Phase 7 (`config/policies/`); the rest stay schema defaults |

## D. Infrastructure files

| # | file:line | Value | Proposed |
|---|---|---|---|
| D1 | `Dockerfile:12` | `python:3.13.7-slim-bookworm` (tag, not digest) | build arg pinned by digest; **closed in Hardening Phase 11** |
| D2 | `Dockerfile:26` | `https://download.pytorch.org/whl/cpu` | build arg `TORCH_INDEX_URL`; **closed in Hardening Phase 11** |
| D3 | `Dockerfile:36-37,51-53` | uid/gid `10001`, port `8000`, healthcheck URL and timings | build args / compose and Helm values; **closed in Hardening Phase 11** (Helm values, kustomize fields, `UVICORN_PORT`) |
| D4 | `docker-compose.yml:13-18` | image tag `oran-adapt:0.1.0`, DB user `oran`, DB name `oran_adapt`, hosts `postgres:5432`, `mlflow:5000`, `kafka:9092` | `.env`-driven variables |
| D5 | `docker-compose.yml` (mlflow) | artifact path `/mlartifacts` (was bucket `mlflow` + MinIO ports `9000`, `9001` until removed in Hardening Phase 0), `--allowed-hosts` list, port `5000` | `.env` variables |
| D6 | `docker-compose.yml` (debezium) | group and topic names, connector name `oran-kpi-sample` | `.env` variables |
| D7 | `docker/mlflow/Dockerfile` | `mlflow==3.16.1`, `psycopg==3.3.6` (boto3 removed with MinIO) | lock file for the MLflow image |
| D8 | `deploy/debezium/kpi-connector.json` | `kpi_sample` table, DB names | templated per CDC source |

## E. Credentials in source

A grep for key, token, password and secret patterns in `src/` found no credentials.
`.env.example` contains the placeholders `change-me` and `change-me-min-8-chars`; these are not
secrets, but Hardening Phase 9 replaces them with secret references. The real `.env` was
deliberately not opened.

## Hardening Phase 1 status

Flat env names below are the keys as they exist today (TOML sections join onto them with `_`,
so `[sandbox.docker] pids_limit` is `SANDBOX_DOCKER_PIDS_LIMIT`). Line numbers in the tables
above are from the baseline and no longer match.

**Closed (the value now comes from a `Settings` key, or from one table):**

| # | Now |
|---|---|
| A1, A2 | `LLM_MAX_OUTPUT_TOKENS`, `LLM_MAX_RETRIES` |
| A3 | `JOB_KILL_GRACE_S` |
| A4-A7 | `SANDBOX_DOCKER_PIDS_LIMIT`, `SANDBOX_DOCKER_CPUS`, `SANDBOX_DOCKER_TMPFS_MB`, `SANDBOX_DOCKER_CLEANUP_TIMEOUT_S` (`sandbox.runner.SandboxLimits`) |
| A8 | `SANDBOX_MANIFEST_MAX_BYTES` |
| A9 | `CDC_MAX_TX_IDS_PER_VERSION` |
| A10, A11 | `CDC_POLLING_TABLE`, `CDC_SCHEMA_REF` (read by both CDC adapters) |
| A12 | `CDC_IDLE_POLL_S` |
| A13, A14 | `DECISION_MEMORY_COPIES`, `DECISION_DEFAULT_CONFIDENCE` |
| A15, A16 | `ANALYSIS_PERFORMANCE_HISTORY_LIMIT`, `API_PAGINATION_DEFAULT_LIMIT` |
| A18, A19 | `REGISTRY_TAGS_CHECKSUM`, `REGISTRY_TAGS_STATUS` (`core.integrity.ArtifactPolicy`, held by the registry adapter) |
| A20, A21 | `API_CORRELATION_HEADER`, `AUTH_API_KEY_HEADER` |
| A22 | explicit: the gate compares `validation.metrics.PRIMARY_METRIC` for the resolved task. A configurable gate metric is Hardening Phase 7 |
| A23 | one table, `core/frameworks.py` (framework -> engine per strategy). Engine selection, the inspector, validation scoring, the drift summary, the CLI and the C8 default read it. `loaders.py` and `registry/client.py` were deleted with the port work |
| B1, B2 | `ARTIFACT_MAX_BYTES`, `ARTIFACT_HASH_CHUNK_BYTES`; the module constants are gone |
| B3, B4 | torch `epochs`/`lr` are required; `run_engine` takes `adaptation.engines.TorchBudget.from_settings(settings)` |
| B5, B6, B7 | `min_psi_rows`, `live_alias`, `sandbox_backend`, `sandbox_docker_image` are required arguments |
| C1, C3 | required when `ENVIRONMENT=production` (with the selected adapters' storage keys since Phase 2) |
| C8 | default derived from `core.frameworks.ADAPTABLE_FRAMEWORKS` |
| C9 | no default; required when `CDC_MODE=kafka` (startup names the key) |

**Kept on purpose:**

- **A17** (histogram buckets): metrics are registered at import, before settings exist, and
  fixed buckets keep histograms from different replicas aggregatable.
- **A24** (`"observed_at"`): the name of the internal KPI table's time column (ORM and schema),
  changed by a migration, not by config. A dataset's own time column is already a parameter
  (`timestamp_column` at onboarding).

**Still open (scheduled later):** C2, C4-C7 and C10-C12 (adapter config and deployment
targets); C13, C14 and the decision engine's significance factors (0.7 / 0.4, floor 0.01) move
to policy files in Hardening Phase 7. The other `oran.*` run tags in `orchestrator/pipeline.py`
are still literals. The framework names in `sandbox/runner.py` and `sandbox/security.py` are the
sandbox's serialization formats and import allow-list, not dispatch. Section D is Hardening
Phase 9.

## Hardening Phase 2 status

**Closed:**

| # | Now |
|---|---|
| C2 | `MLFLOW_TRACKING_URI` is the `mlflow` registry adapter's config: listed in its `Capability.config_keys`, required only when `REGISTRY_BACKEND=mlflow`, and demanded by `ENVIRONMENT=production` only then (`Capability.production_keys`). A production deployment on another registry sets that registry's storage keys instead (`REGISTRY_FS_ROOT`, plus `ARTIFACT_STORE_ROOT` for the filesystem artifact store). No module outside `adapters/registry/mlflow/` imports MLflow (import-boundary test) |

**Changed but still open:**

- **C12** (`MLFLOW_SKOPS_TRUSTED_TYPES`) is no longer MLflow-specific in use: the `native` and
  `mlflow-flavors` handlers and the sandbox all read it. Only the name is vendor-prefixed. Renaming
  it to `handlers.sklearn.trusted_types` needs a deprecation alias and is left for Hardening
  Phase 12 (config migration).
- **C7** (`LIVE_ALIAS` / `CANDIDATE_ALIAS`): aliases are now a registry-port concept every adapter
  implements (conformance check `aliases`), so they are no longer MLflow semantics. Moving them
  under deployment targets is Hardening Phase 3.

## Hardening Phase 3 status

**Closed:**

| # | Now |
|---|---|
| C7 | What serves traffic is the deployment port's business (`DEPLOYMENT_BACKEND`), not an alias convention. `LIVE_ALIAS` / `CANDIDATE_ALIAS` remain the registry's record of which version was promoted. "Serving reads the live alias" is now just the default adapter, `registry-alias`, whose alias is its own key (`DEPLOYMENT_ALIAS`, falling back to `LIVE_ALIAS`, refused if it equals `CANDIDATE_ALIAS`). There is one deployment target per installation, not a list of `deployment.targets[]`. Several targets at once would be a new adapter (a fan-out like the registry `mirror`); none was asked for |

## Hardening Phase 4 status

No baseline item was open in this area: there were no outbound notifications to hardcode. The
new code (`notifications/`, `adapters/notify*.py`) takes every sink address, credential,
timeout, retry, backoff, breaker, lease, batch and event-routing value from `NOTIFICATION_*`
keys (`docs/adapters/notification.md`, "Configuration"). What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `adapters/notify_brokers.py` (`NatsSink`) | port `4222` when `NOTIFICATION_NATS_URL` has none | the NATS protocol's registered default port; set a port in the URL to change it |
| `adapters/notify.py` (`PagerDutySink.payload`) | summary cut to 1024 characters | PagerDuty Events API v2 limit on `payload.summary` |
| `adapters/notify.py` (`is_retryable_status`) | 5xx, 408, 425, 429 retryable | HTTP semantics of "try again later" |
| `notifications/dispatcher.py` | `leased_by` cut to 100 characters; `last_error` to 2000 | the `leased_by` column width; a bound on stored error text |
| `adapters/notify_email.py` | subject cut to 200 characters | mail clients truncate long subjects; the full text is in the body |
| `notifications/service.py` | at most 100 delivery ids in one redrive audit row | a bound on the audit row's size (the count is always recorded) |

## Hardening Phase 5 status

No baseline item reopened. The new code (`datastore/access.py`, `datastore/formats.py`,
`adapters/datasets*.py`, `cdc/triggers.py`) takes every allow-list, limit, batch size, timeout,
endpoint and credential from `DATASET_*` keys (`docs/adapters/dataset.md`, "Configuration"),
and the CDC column names from `CDC_*_COLUMN` / `CDC_DATASET_ID`. A24 (`observed_at`) is now
only the default of `DATASET_TIME_COLUMN` and `CDC_TIME_COLUMN` for external data; it stays
the internal `data_record` column name. C10 (`CDC_KAFKA_TOPIC` defaulting to the kpi_sample
topic) is unchanged: the default still names that table, and following another table means
setting the topic. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `datastore/formats.py` | suffixes `.parquet`/`.pq`, `.csv`, `.jsonl`/`.ndjson` | file-format conventions; `format` overrides them per version |
| `cdc/triggers.py` | identifiers limited to `[A-Za-z_][A-Za-z0-9_]{0,62}` | they are interpolated into DDL, and 63 is PostgreSQL's identifier limit |
| `api/routes_data.py` | `limit` at most 1000 rows per page; `storage_uri` at most 500 characters | a bound on one response; the `storage_uri` column width |

## Hardening Phase 6 status

No baseline item reopened. The worker, reaper, executors and queue adapters
(`orchestrator/worker.py`, `adapters/job_executors.py`, `adapters/job_queues.py`) take every
interval, threshold, class, priority, tenant limit, queue name, broker URL and pod spec from
`JOB_*` keys (`docs/adapters/job_queue.md`, "Configuration"). C14 still covers the `JOB_*`
schema defaults. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `adapters/job_queues.py` | Kubernetes Job name `<K8S_NAME_PREFIX>oran-job-<job_id>-a<attempt>` | it must be unique per attempt and fit the 63-character DNS label; the job id makes it unique |
| `orchestrator/worker.py` | lease tokens and worker ids from `uuid4().hex` | identifiers, not tunables |
| `api/routes_adaptation.py` | `limit` at most 1000 jobs per page | a bound on one response, as on the other list routes |
| `docker-compose.yml` (worker) | `stop_grace_period: 45s` | must exceed `JOB_DRAIN_TIMEOUT_S` (30 s); it belongs with D4 (compose values) |

## Hardening Phase 7 status

C13 is closed: `VALIDATION_ACCURACY_TOLERANCE` and `VALIDATION_RMSE_TOLERANCE_RATIO` are removed.
Every threshold of the validation gate and of progressive delivery is a field of the versioned
`GatePolicy` / `DeliveryPolicy` (`core/policies.py`), set inline (`GATE_POLICY`,
`DELIVERY_POLICY`) or from a TOML/JSON file (`config/policies/gate.toml`, `delivery.toml`); the
Prometheus source takes its URL, queries, step and timeout from `ROLLOUT_PROMETHEUS_*`. C14
stays open for the `ANALYSIS_*`, `REUSE_*`, `DECISION_*`, `TORCH_*` and `JOB_*` schema
defaults. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `delivery/controller.py` | rollout ids from `uuid4().hex`; lock holder `rollout:<id>`; idempotency key `rollout:<id>:promote` | identifiers, not tunables |
| `delivery/controller.py` | the strategy names and rollout states (`SHADOW`, `CANARY`, `AB`, `AWAITING_APPROVAL`, ...) | the state machine's vocabulary, stored in the database |
| `api/routes_rollouts.py` | at most 50 metrics per observation; `limit` at most 1000 rollouts per page | bounds on one request and one response, as on the other routes |
| `validation/gate.py` | the metric per task type (accuracy, RMSE, ...) and its direction | the definition of the task type; `metric_guards` adds others |

## Hardening Phase 8 status

No baseline item reopened, and the counts do not move (C8 was closed in Phase 1). C8's derived
default no longer lists frameworks at all: `DECISION_SUPPORTED_FRAMEWORKS` defaults to `[]`,
which the decision layer reads as every framework an installed model type plugin can adapt
(`supported_frameworks`). A23's single table (`core/frameworks.py`) is now read only by the
built-in tabular and sequence plugins; everywhere else (pipeline, validation, reuse check,
decision, drift summary, CLI) asks the model type registry (`adaptation/model_types.py`), and a
new framework is a new plugin, not a row in that table. The plugins, their order (`MODEL_TYPES`), sequence training (`SEQUENCE_*`) and the ONNX runtime
(`ONNX_RUNTIME`) are keys. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `adapters/model_types/*.py` | each plugin's framework names (`sklearn`, `lightgbm`, `torch`, ...) and engine names | the plugin's identity: what it serves; a new framework is a new plugin |
| `adapters/model_types/sequence.py` | the layer types that mark a torch module as sequential (recurrent, convolutional, attention) | how the plugin recognises its models; a module can declare `sequence_window` instead |
| `adapters/model_types/onnx.py` | output names `label`/`probabilities` and ArgMax as the classifier signal | ONNX conventions (skl2onnx, onnxmltools) |
| `adapters/model_types/tabular.py` | continued boosting via `init_model` | the LightGBM and CatBoost API |

## Hardening Phase 9 status

No baseline item reopened, and the counts do not move: no C or D item is about security.
Identity (`AUTH_BACKEND` and the `AUTH_*` keys), secrets (`SECRETS_BACKEND`, `SECRETS_VAULT_*`),
rate limits (`API_RATE_LIMIT_*`), docs and HSTS (`API_DOCS_ENABLED`, `API_HSTS_MAX_AGE_S`) and
the outbound policy (`OUTBOUND_*`) are keys. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `api/app.py` | security header values (`nosniff`, `DENY`, `no-referrer`, `no-store`, CSP `default-src 'none'`) | the hardening itself; only HSTS depends on the deployment |
| `api/security.py` | `DOCS_PATHS` | FastAPI's docs routes; whether they are served is `API_DOCS_ENABLED` |
| `adapters/jwt.py` | supported algorithm families; `none` and HMAC refused | the security rule; the allowed set is `AUTH_JWT_ALGORITHMS` |
| `adapters/auth.py` | `/.well-known/openid-configuration`, Envoy XFCC `Cert=` | OpenID Connect Discovery and Envoy formats |
| `adapters/vault.py` | `/v1/<mount>/data/<path>`, `X-Vault-Token`, `X-Vault-Namespace` | the Vault KV v2 API; mount and path are keys |
| `core/outbound.py` | non-public address classes | `ipaddress` classification; exceptions go in `OUTBOUND_ALLOWLIST` |
| `scripts/secret_scan.py` | credential patterns | the scanner's rules, in one table |

## Hardening Phase 10 status

No baseline item reopened, and the counts do not move. A1 and A2 were already keys. C4 and C5
(the default model ids) stay open. They apply only once `LLM_ENABLED=true`, and they are
recorded in `docs/OPEN-QUESTIONS.md` ("LLM"). The switch (`LLM_ENABLED`), the providers' base
URLs, the guard (`LLM_MAX_*`, `LLM_*_BUDGET`, `LLM_BREAKER_*`, `LLM_RETRY_BACKOFF_S`,
`LLM_COST_PER_1K_*`, `LLM_CHARS_PER_TOKEN`), the prompts (`LLM_PROMPT_VERSIONS`,
`LLM_PROMPT_DIR`), `MLFLOW_TELEMETRY` and `MLFLOW_PIP_REQUIREMENTS` are keys. What is still a
literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `llm/prompts.py` | the text of `strategy-selection@1`, `adaptation-code@1` | versioned built-ins; replaced through `LLM_PROMPT_DIR` and `LLM_PROMPT_VERSIONS` |
| `adapters/llm_providers.py` | `/chat/completions`, message roles | the OpenAI chat completions wire format |
| `llm/guard.py`, `decision/llm_selector.py` | fallback reason names | the recorded vocabulary (`docs/adapters/llm.md`) |
| `adapters/registry/mlflow/registry.py` | `MLFLOW_DISABLE_TELEMETRY` | MLflow's own switch; whether it is set is `MLFLOW_TELEMETRY` |

## Hardening Phase 11 status

D1, D2 and D3 are closed: every image base and every image compose pulls is pinned by digest;
the torch index is the build arg `TORCH_INDEX_URL`; the UID, port, probes and grace periods are
Helm values and kustomize fields (`docs/operations/helm.md`). D4-D6 stay open: they are the
development compose stack's own topology. D7 (the MLflow image's pins) and D8 (the Debezium
connector) are unchanged. What is still a literal, and why:

| Where | Value | Why it is not a key |
|---|---|---|
| `Dockerfile` | UID/GID 10001, `/tmp/oran-adapt/...` paths | the image's identity and layout; the chart's `podSecurityContext` and the `/tmp` volume set them per deployment |
| `deploy/helm/oran-adapt/values.yaml`, `deploy/kustomize/base/` | replicas, resources, probe timings, grace periods | defaults, each a value or a patchable field |
| `deploy/helm/oran-adapt/examples/`, `deploy/kustomize/overlays/` | namespaces, `example.com` hosts, all-zero digests | examples of one environment, marked as placeholders |
| `deploy/helm/oran-adapt/files/prometheus-rules.yaml` | alert thresholds and `for:` windows | alerts as code, each with a runbook in `docs/runbooks/` |

## Burn-down counters

| Category | Count at baseline | Open after Phase 1 | Open after Phase 2 | Open after Phase 3 | Open after Phase 4 | Open after Phase 5 | Open after Phase 6 | Open after Phase 7 | Open after Phase 8 | Open after Phase 9 | Open after Phase 10 | Open after Phase 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A (use-site literals) | 24 | 0 (A17, A24 kept, see above) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| B (duplicated defaults) | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| C (settings needing a decision) | 14 | 10 | 9 | 8 | 8 | 8 | 8 | 7 | 7 | 7 | 7 | 7 |
| D (infrastructure) | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 5 |
