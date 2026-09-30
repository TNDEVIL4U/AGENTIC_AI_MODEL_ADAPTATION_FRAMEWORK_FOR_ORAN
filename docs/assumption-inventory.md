# Assumption inventory (Hardening Phase 0 baseline)

Every place the code, at commit `2893513`, assumes a particular stack or shape. Each row names
the hardening finding (#1–#10) it feeds and the phase expected to remove it.

## 1. Assumes MLflow is the registry (finding #1, Hardening Phase 2)

| Where | Assumption |
|---|---|
| `registry/client.py` (whole module) | `MlflowRegistry` is the only registry implementation, with no interface in front of it |
| `api/app.py:144`, `cli.py:49`, `orchestrator/jobs.py:315` | constructs `MlflowRegistry` directly (3 composition points, not 1) |
| `orchestrator/jobs.py:273` | `isinstance(registry, MlflowRegistry)` to decide how to rebuild it in the worker process |
| `orchestrator/pipeline.py:229`, `analysis/version_eval.py:38,54,119`, `registry/promotion.py:89-316`, `registry/onboarding.py:71,169`, `orchestrator/jobs.py` (7 signatures) | typed against the concrete `MlflowRegistry` class |
| `db/models.py` (`model_metadata.mlflow_model_name`) + 30 uses (onboarding 13, pipeline 5, routes_models 4, cli 4, promotion 3) | the registry name is an MLflow name, baked into the DB schema |
| `api/routes_health.py:29` | readiness requires MLflow (`registry.ping`) |
| `registry/client.py:142` | `models:/{name}/{version}` URI scheme |
| `core/config.py` | `MLFLOW_TRACKING_URI`, `MLFLOW_REGISTRY_URI`, `MLFLOW_SKOPS_TRUSTED_TYPES` as top-level settings |

## 2. Assumes model loading goes through MLflow flavours (findings #1 and #10, Hardening Phases 2 and 8)

| Where | Assumption |
|---|---|
| `adaptation/loaders.py:14-38` | `mlflow.sklearn/xgboost/pytorch.load_model`; any other framework is `UnsupportedAdaptationError` |
| `registry/client.py:199` | only `sklearn, xgboost, torch, pytorch` can be logged |

## 3. Assumes the `live` alias is deployment (findings #2 and #6, Hardening Phases 3 and 7)

**Closed in Hardening Phases 3 and 7** (`docs/PHASE3_REPORT.md`, `docs/PHASE7_REPORT.md`):
promotion rolls out to a serving system through `DeploymentPort` and reads it back (Phase 3).
The gate requires a statistically significant improvement with guardrails and records every
decision under a versioned policy. A candidate then reaches traffic as a shadow, canary, A/B or
manually approved rollout, which rolls back automatically on online health evidence (Phase 7).
The table below is the baseline as audited.

| Where | Assumption |
|---|---|
| `registry/promotion.py:97-370` | "promote" = `set_alias(LIVE_ALIAS)` plus a status tag. Nothing reaches a serving layer and nothing is verified afterwards |
| `orchestrator/pipeline.py:242,379,444,646,694` | reads and writes `live_alias` / `candidate_alias` |
| `registry/onboarding.py:83,135,176-190` | onboarding sets `live` |
| `api/routes_models.py:87-218`, `cli.py:175,193` | expose and roll back the alias |
| whole pipeline | one step switches 100% of traffic: no shadow, canary, blue/green or approval |
| `validation/engine.py:69-77` | the gate passes a candidate up to the tolerance **worse** than the current model; the primary metric is the first dict key; no statistical test and no guardrails |

## 4. Assumes data arrives as JSON rows (finding #4, Hardening Phase 5)

**Closed in Hardening Phase 5** (`docs/PHASE5_REPORT.md`): a version can be a reference to an
object read in batches through `DatasetPort` adapters, with a memory ceiling, sampling for
analysis, streamed hashing and any time column; CDC follows any table through a column
mapping. The table below is the baseline as audited.

| Where | Assumption |
|---|---|
| `api/routes_data.py:40-43,68-92` | `VersionCreate.records: list[dict]` inline in the body, capped only by `API_MAX_REQUEST_BYTES` |
| `db/models.py` (`data_record.payload`) | every row stored as one JSON DB row |
| `analysis/retrieval.py:52-72`, `adaptation/data.py`, `datastore/versioning.py:452-485` | `.all()` loads a whole data version into Python lists or DataFrames: no streaming, chunking or memory ceiling |
| `datastore/versioning.py:91-104` | content hash computed over the whole canonical JSON in memory |
| `cdc/sources.py:40`, migrations 0005/0006, `deploy/debezium/kpi-connector.json`, `CDC_KAFKA_TOPIC` | CDC reads only the `kpi_sample` table with `{dataset_id, observed_at, payload}` columns |
| `analysis/retrieval.py:64`, `adaptation/data.py` | the time column is always named `observed_at` |
| `datastore/versioning.py` (`storage_uri` column) | exists but is not used to read data by reference |

## 5. Assumes a single process (finding #5, Hardening Phase 6)

**Closed in Hardening Phase 6** (`docs/PHASE6_REPORT.md`): the API only queues a job and a
separate worker claims it under a lease, with fencing, a reaper, deadlines and cancellation
that kill the whole process tree, retries by requeue, poison quarantine, graceful drain, worker
classes, priorities and per-tenant limits. Queue adapters: `database`, `celery`, `rq`,
`kubernetes`, `inline` (development only). The Prometheus registry is still per process
(below); it is scraped per pod. The table below is the baseline as audited.

| Where | Assumption |
|---|---|
| `api/routes_adaptation.py:23-41` | the HTTP request **blocks until the job finishes**; a long job holds an API threadpool slot and the client connection |
| `orchestrator/jobs.py:382-445` | the job runs in a child process supervised by the API process. If the API process dies, nothing supervises the child or records its outcome or timeout, and the job stays non-terminal until the model lock TTL expires and the next event for that model takes it over (`_fail_abandoned_job`) |
| `orchestrator/jobs.py:447-481` | retries `time.sleep` on the request thread |
| `orchestrator/jobs.py:224-268` | thread mode cannot stop a timed-out job |
| `core/metrics.py` | the Prometheus registry lives in each process, with no multiprocess mode, so N replicas give N disjoint counter sets (Prometheus can still sum them per pod) |
| no job queue | no cancellation, priorities, fairness or GPU scheduling; one job per model at a time via `model_lock` |

Parts that already work across processes: idempotency (unique DB constraint) and model locks
(DB rows with TTL). Both survive restarts and multiple replicas sharing one PostgreSQL.

## 6. Assumes tabular data and specific model families (finding #10, Hardening Phase 8)

| Where | Assumption |
|---|---|
| `adaptation/inspector.py:84-89` | only sklearn-like and torch; anything else raises `UnsupportedAdaptationError` (typed, not a crash) |
| `adaptation/inspector.py:57-68` | torch task is inferred from the first and last `nn.Linear` (feed-forward only); RNN, CNN and transformer models are misread |
| `adaptation/torch_engine.py:23-27`, `validation/evaluate.py:41` | `X.to_numpy()` into one full-batch float tensor: a flat feature matrix |
| `adaptation/data.py:68-77` | `split_features_target(df, feature_names, target_column)`: a flat feature matrix plus one target column |
| `adaptation/leakage.py`, `validation` hold-out | temporal split on `observed_at` (good), but no windowing or lag construction |
| framework string sets in 6 files (see `hardcoding-inventory.md` A23) | dispatch by `if fw in {...}` instead of a handler registry |
| `core/enums.py:TaskType` | declares FORECASTING, CLUSTERING and ANOMALY_DETECTION, but no engine exists for forecasting |

**Closed in Hardening Phase 8** (`docs/PHASE8_REPORT.md`): the framework-name tables are gone.
Inspection, adaptation, scoring, the version reuse check, the decision layer and the drift
summary go through one model type registry (`adaptation/model_types.py`) of plugins behind
`ModelTypePort`, found through the `oran_adapt.model_type` entry-point group. Plugins cover
sklearn, XGBoost, LightGBM, CatBoost, torch tabular models, torch sequence models (RNN, LSTM,
GRU, TCN, transformer) over sliding windows, Keras, ONNX (scoring only) and statsmodels state
space forecasters. Temporal models keep rows in time order: the hold-out is the newest rows,
windowing lives inside the plugin, and the conformance suite adapts them with every random
reordering disabled. A model no plugin serves gets a typed `unsupported_model_type` result,
never a crash. LightGBM, CatBoost and Keras are exercised only with test doubles, and ONNX only
with the `onnx` reference evaluator: unverified locally against the real libraries.

## 7. Assumes internet access (finding #8, Hardening Phase 10)

| Where | Assumption |
|---|---|
| `llm/client.py` | Anthropic or Gemini SaaS endpoints; no self-hosted or OpenAI-compatible endpoint option |
| `.env.example:4` | ships `LLM_PROVIDER=anthropic`. Copying it unchanged without a key makes **startup fail** (`core/config.py` validator), so the example is not offline-safe even though the code default is `none` |
| `decision/llm_selector.py:109-121` | an LLM failure, or invalid JSON or schema, falls back to rules (good). There is no circuit breaker, no token or cost cap, and no prompt versioning |
| `adaptation/llm_adapter.py` | LLM-generated adapter code runs in the sandbox when no engine fits; LLM unavailability propagates as `LlmUnavailableError` |
| `Dockerfile:26` | build pulls from `download.pytorch.org` and PyPI (build time only) |

**Closed in Hardening Phase 10** (`docs/PHASE10_REPORT.md`): the LLM is off unless
`LLM_ENABLED=true`, and `.env.example` ships `LLM_ENABLED=false`, `LLM_PROVIDER=none`, so
copying it starts offline. Providers are adapters: `anthropic`, `gemini` and
`openai-compatible` for self-hosted servers, each with a base-URL key, plus plugins. Every call
goes through `llm/guard.py`: an input cap, a token and cost budget shared through the
`llm_usage` table, a per-provider circuit breaker, retries with backoff, and a usage row per
call. Prompts are versioned and stamped on the decision and the job. Every failure, including
unsafe or failing generated code, falls back to the rules or to native retraining with a
recorded reason; no job fails for it. The egress-blocked end-to-end test also found MLflow's
usage telemetry, which is now off (`MLFLOW_TELEMETRY`). What remains is the image build: it
downloads packages at build time. The running service needs no internet. The live provider
services are unverified locally.

## 8. Assumes API keys are the only identity (finding #9, Hardening Phase 9)

| Where | Assumption |
|---|---|
| `api/security.py` | SHA-256 API keys from `API_KEYS`; no OIDC, mTLS or gateway identity |
| `api/security.py:3-10`, route decorators | role checks are attached per route, not by a central deny-by-default matrix. A new route inherits only the router's READ dependency |
| `core/config.py` | secrets come from env or `.env` only; there is no secret manager |
| none | no egress allowlist or SSRF checks (no outbound URLs are user-supplied yet) |

**Closed in Hardening Phase 9** (`docs/PHASE9_REPORT.md`): identity is behind `AuthPort`, with
`oidc` (JWTs verified against the issuer's JWKS), `gateway` (signed assertions from a trusted
proxy) and `mtls` (forwarded client certificates) beside `api-key`. Authorization is
deny-by-default: every route declares `public` or `require(action)` or the API refuses to start,
and `docs/security/authz-matrix.md` is generated from the routes served. Secrets come from
`SecretsPort` (`env`, `file`, `vault`). Every outbound HTTP client goes through one SSRF/TLS
policy (`core/outbound.py`). Rate limits, security headers and a secret scan were added. A real
IdP, Vault and proxy, and the CI-only CVE scan and SBOM, are unverified locally.

## 9. Assumes it was never packaged (finding #7, Hardening Phases 0 and 11)

| Where | Assumption |
|---|---|
| `Dockerfile`, `docker/*/Dockerfile`, `docker-compose.yml` | never built or run |
| `requirements.lock` | generated on Windows and applied as constraints on Linux |
| none | no Helm chart, Kubernetes manifests or CI before Hardening Phase 0 |
