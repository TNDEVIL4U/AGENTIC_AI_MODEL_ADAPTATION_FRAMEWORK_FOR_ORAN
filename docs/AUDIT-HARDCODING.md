# Hardcoding audit

This file lists every literal from the baseline inventory (`docs/hardcoding-inventory.md`,
sections A–D) and where its value comes from now. The inventory keeps the history phase by
phase, and this file is the final state.

**Remaining: 0.** Every row is `closed` or `kept`. A `kept` row is not a tunable, and its
reason is given. The burn-down counters in the inventory read A 0, B 0, C 0, D 0 after Hardening
Phase 15.

`scripts/audit.py` checks this file (see "How this is checked" at the end). `scripts/verify.sh
15` and `scripts/verify.sh all` run it.

Keys are the flat environment names. A TOML section joins onto its key with `_`, so
`[sandbox.docker] pids_limit` is `SANDBOX_DOCKER_PIDS_LIMIT`. Paths are relative to the
repository root.

## A. Use-site literals

| ID | Baseline literal | Where it lives now | Keys | Status |
|---|---|---|---|---|
| A1 | `max_tokens=2048` (LLM response cap) | `src/oran_adapt/core/config.py`, read by `src/oran_adapt/llm/client.py` | `LLM_MAX_OUTPUT_TOKENS` | closed |
| A2 | `max_retries=0` (LLM SDK retries) | `src/oran_adapt/llm/guard.py` (the guard retries, the SDK does not) | `LLM_MAX_RETRIES` | closed |
| A3 | `_KILL_GRACE_S = 5.0` | `src/oran_adapt/adapters/job_executors.py` | `JOB_KILL_GRACE_S` | closed |
| A4 | `--pids-limit 128` | `src/oran_adapt/sandbox/runner.py` (`SandboxLimits`) | `SANDBOX_DOCKER_PIDS_LIMIT` | closed |
| A5 | `--cpus 1` | `src/oran_adapt/sandbox/runner.py` (`SandboxLimits`) | `SANDBOX_DOCKER_CPUS` | closed |
| A6 | `size=64m` (sandbox tmpfs) | `src/oran_adapt/sandbox/runner.py` (`SandboxLimits`) | `SANDBOX_DOCKER_TMPFS_MB` | closed |
| A7 | `timeout=60` (`docker rm`) | `src/oran_adapt/sandbox/runner.py` (`SandboxLimits`) | `SANDBOX_DOCKER_CLEANUP_TIMEOUT_S` | closed |
| A8 | `_MANIFEST_MAX_BYTES = 4096` | `src/oran_adapt/sandbox/runner.py` | `SANDBOX_MANIFEST_MAX_BYTES` | closed |
| A9 | `_MAX_TX_IDS = 1000` | `src/oran_adapt/cdc/materialize.py` (a parameter, passed from `src/oran_adapt/api/routes_data.py`) | `CDC_MAX_TX_IDS_PER_VERSION` | closed |
| A10 | `table="kpi_sample"` (CDC polling source) | `src/oran_adapt/adapters/polling_cdc.py` | `CDC_POLLING_TABLE` | closed |
| A11 | `"kpi_sample/1"` (CDC payload schema) | `src/oran_adapt/adapters/polling_cdc.py`, `src/oran_adapt/adapters/kafka_cdc.py` | `CDC_SCHEMA_REF` | closed |
| A12 | `idle_sleep_s=1.0` | `src/oran_adapt/cdc/consumer.py` | `CDC_IDLE_POLL_S` | closed |
| A13 | `_MEMORY_COPIES = 3` | `src/oran_adapt/decision/report.py` | `DECISION_MEMORY_COPIES` | closed |
| A14 | `confidence=0.5` | `src/oran_adapt/decision/engine.py` | `DECISION_DEFAULT_CONFIDENCE` | closed |
| A15 | `.limit(10)` (performance records per job) | `src/oran_adapt/analysis/engine.py` | `ANALYSIS_PERFORMANCE_HISTORY_LIMIT` | closed |
| A16 | `limit=50` (`GET /current-data` page) | `src/oran_adapt/api/routes_data.py` and the other list routes | `API_PAGINATION_DEFAULT_LIMIT` | closed |
| A17 | `_EVAL_BUCKETS` (histogram buckets) | `src/oran_adapt/core/metrics.py` | – | kept: metrics are registered at import, before settings exist, and fixed buckets keep histograms from different replicas aggregatable |
| A18 | `"artifact.sha256"` (checksum tag) | `src/oran_adapt/core/integrity.py` (`ArtifactPolicy`) | `REGISTRY_TAGS_CHECKSUM` | closed |
| A19 | `"oran.status"` (status tag) | `src/oran_adapt/core/integrity.py` (`ArtifactPolicy`) | `REGISTRY_TAGS_STATUS` | closed |
| A20 | `"X-Correlation-ID"` | `src/oran_adapt/api/app.py` | `API_CORRELATION_HEADER` | closed |
| A21 | `"X-API-Key"` | `src/oran_adapt/adapters/access.py` | `AUTH_API_KEY_HEADER` | closed |
| A22 | `next(iter(current_metrics))` (primary metric = first dict key) | `src/oran_adapt/validation/metrics.py` (`PRIMARY_METRIC` per task), compared by `src/oran_adapt/validation/gate.py` | `GATE_POLICY` | closed: the primary metric is explicit per task, and every threshold is in the versioned gate policy |
| A23 | `{"sklearn","xgboost"}`, `{"torch","pytorch"}` name tables in six places | `src/oran_adapt/adaptation/model_types.py` and the plugins in `src/oran_adapt/adapters/model_types/` | `MODEL_TYPES` | closed |
| A24 | `"observed_at"` (time column) | `src/oran_adapt/db/models.py` (the internal table's column) | `DATASET_TIME_COLUMN`, `CDC_TIME_COLUMN` | kept: `observed_at` is the internal KPI table's column name, changed by a migration and not by config. External data names its own time column through the keys |

## B. Duplicated defaults

| ID | Baseline literal | Where it lives now | Keys | Status |
|---|---|---|---|---|
| B1 | `DEFAULT_ARTIFACT_MAX_BYTES = 2 GiB` | `src/oran_adapt/core/integrity.py` (`ArtifactPolicy`; the constant is gone) | `ARTIFACT_MAX_BYTES` | closed |
| B2 | `_CHUNK = 1 MiB` | `src/oran_adapt/core/integrity.py` (`ArtifactPolicy`) | `ARTIFACT_HASH_CHUNK_BYTES` | closed |
| B3 | `epochs=5, lr=1e-2` (fine-tune) | `src/oran_adapt/adaptation/torch_engine.py` (required arguments, from `TorchBudget`) | `TORCH_FINE_TUNE_EPOCHS`, `TORCH_LEARNING_RATE` | closed |
| B4 | `epochs=300, lr=1e-2` (full retrain) | `src/oran_adapt/adaptation/torch_engine.py` (required arguments, from `TorchBudget`) | `TORCH_FULL_RETRAIN_EPOCHS`, `TORCH_LEARNING_RATE` | closed |
| B5 | `min_psi_rows=30` | `src/oran_adapt/analysis/reuse.py` (a required argument) | `ANALYSIS_MIN_PSI_ROWS` | closed |
| B6 | `live_alias="live"` | `src/oran_adapt/registry/onboarding.py` (a required argument) | `LIVE_ALIAS` | closed |
| B7 | `sandbox_backend="subprocess"`, `sandbox_docker_image=""` | `src/oran_adapt/adaptation/llm_adapter.py` (required arguments) | `SANDBOX_BACKEND`, `SANDBOX_DOCKER_IMAGE` | closed |

## C. Settings that needed a decision

| ID | Baseline literal | Where it lives now | Keys | Status |
|---|---|---|---|---|
| C1 | `DATABASE_URL=sqlite:///./data/oran_adapt.db` | `src/oran_adapt/core/config.py`: a development default, required when `ENVIRONMENT=production` | `DATABASE_URL`, `ENVIRONMENT` | closed |
| C2 | `MLFLOW_TRACKING_URI=sqlite:///./data/mlflow.db` | the `mlflow` registry adapter's config (`src/oran_adapt/adapters/registry/`), required only with that adapter | `MLFLOW_TRACKING_URI`, `REGISTRY_BACKEND` | closed |
| C3 | `ARTIFACT_WORKDIR=./data/artifacts` | `src/oran_adapt/core/config.py`: a development default, required when `ENVIRONMENT=production` | `ARTIFACT_WORKDIR`, `ENVIRONMENT` | closed |
| C4 | `ANTHROPIC_MODEL=claude-sonnet-5` | no default; `src/oran_adapt/adapters/llm_providers.py` refuses to build the provider without it | `ANTHROPIC_MODEL` | closed in Phase 15 |
| C5 | `GEMINI_MODEL=gemini-3.6-flash` | no default; `src/oran_adapt/adapters/llm_providers.py` refuses to build the provider without it | `GEMINI_MODEL` | closed in Phase 15 |
| C6 | `SANDBOX_DOCKER_IMAGE=oran-adapt-sandbox:latest` | no default; `Settings` requires it with `SANDBOX_BACKEND=docker`, pinned by digest (`name@sha256:...`) | `SANDBOX_DOCKER_IMAGE`, `SANDBOX_BACKEND` | closed in Phase 15 |
| C7 | `LIVE_ALIAS` / `CANDIDATE_ALIAS` as "what serves" | the deployment port (`src/oran_adapt/registry/deployment.py`); the aliases are only the registry's record | `DEPLOYMENT_BACKEND`, `DEPLOYMENT_ALIAS`, `LIVE_ALIAS`, `CANDIDATE_ALIAS` | closed |
| C8 | `DECISION_SUPPORTED_FRAMEWORKS=sklearn,xgboost,torch,pytorch` | the default `[]` means every framework an installed model-type plugin can adapt (`src/oran_adapt/adaptation/model_types.py`) | `DECISION_SUPPORTED_FRAMEWORKS`, `MODEL_TYPES` | closed |
| C9 | `KAFKA_BOOTSTRAP_SERVERS=localhost:9092` | no default; required with `CDC_MODE=kafka` | `KAFKA_BOOTSTRAP_SERVERS`, `CDC_MODE` | closed |
| C10 | `CDC_KAFKA_TOPIC=oran.public.kpi_sample` | no default; `src/oran_adapt/adapters/kafka_cdc.py` requires it with `CDC_MODE=kafka`. Compose derives it from `CDC_TOPIC_PREFIX` and `CDC_SOURCE_TABLE` | `CDC_KAFKA_TOPIC` | closed in Phase 15 |
| C11 | `CDC_CONSUMER_GROUP=oran-adapt-cdc` | no default; required with `CDC_MODE=kafka`. A shared default group would split one topic's partitions between unrelated installations | `CDC_CONSUMER_GROUP` | closed in Phase 15 |
| C12 | `MLFLOW_SKOPS_TRUSTED_TYPES` (vendor-prefixed name) | `src/oran_adapt/core/config.py`, read by every handler and the sandbox | `MLFLOW_SKOPS_TRUSTED_TYPES` | closed in Phase 15 (decision): the name stays. It names the skops serialization format the list applies to, which MLflow and the native handler both use, and renaming it would break every deployment that sets it for no functional gain |
| C13 | `VALIDATION_ACCURACY_TOLERANCE` / `VALIDATION_RMSE_TOLERANCE_RATIO` | removed; the versioned gate policy (`src/oran_adapt/core/policies.py`, `config/policies/gate.toml`) | `GATE_POLICY`, `DELIVERY_POLICY` | closed |
| C14 | threshold and budget keys (`ANALYSIS_*`, `REUSE_*`, `DECISION_*`, `TORCH_*`, `JOB_*`) | typed `Settings` fields with bounds (`src/oran_adapt/core/config.py`), every one overridable, and shown with its source by `oran-adapt config effective` | `ANALYSIS_MIN_PSI_ROWS`, `TORCH_FULL_RETRAIN_EPOCHS`, `JOB_TIMEOUT_S` | closed in Phase 15 (decision): schema defaults are the intended home. Validation and delivery thresholds moved to policy files in Phase 7 |

## D. Infrastructure

| ID | Baseline literal | Where it lives now | Keys | Status |
|---|---|---|---|---|
| D1 | `python:3.13.7-slim-bookworm` (a tag, not a digest) | `Dockerfile`, a build argument pinned by digest | `PYTHON_IMAGE` | closed |
| D2 | `https://download.pytorch.org/whl/cpu` | `Dockerfile`, a build argument | `TORCH_INDEX_URL` | closed |
| D3 | uid/gid `10001`, port `8000`, healthcheck URL and timings | `Dockerfile`, `deploy/helm/oran-adapt/values.yaml`, `deploy/kustomize/` | `API_PORT` | closed |
| D4 | image tag `oran-adapt:0.1.0`, DB user `oran`, DB name `oran_adapt` | `docker-compose.yml`, as variables with defaults, listed in `.env.example` | `ORAN_ADAPT_VERSION`, `POSTGRES_USER`, `POSTGRES_DB` | closed in Phase 15 |
| D5 | MLflow `--allowed-hosts` list, port `5000`, image tag | `docker-compose.yml`, as variables with defaults | `MLFLOW_ALLOWED_HOSTS`, `MLFLOW_PORT`, `MLFLOW_IMAGE_TAG` | closed in Phase 15 |
| D6 | Connect group and topic names, connector name `oran-kpi-sample` | `docker-compose.yml`, as variables with defaults | `CONNECT_GROUP_ID`, `CONNECT_TOPIC_PREFIX`, `CDC_CONNECTOR_NAME` | closed in Phase 15 |
| D7 | `mlflow==3.16.1`, `psycopg==3.3.6` inline in the MLflow Dockerfile | `docker/mlflow/requirements.txt`; `tests/unit/test_phase15_audit.py` keeps it equal to `requirements.lock` | `MLFLOW_IMAGE_TAG` | closed in Phase 15 |
| D8 | the `kpi_sample` table and DB names in the connector | `deploy/debezium/kpi-connector.json`, through Kafka Connect's `EnvVarConfigProvider` (`${env:...}`) | `POSTGRES_USER`, `POSTGRES_DB`, `CDC_TOPIC_PREFIX`, `CDC_SOURCE_TABLE` | closed in Phase 15 |

Some names in the compose file stay literal. The service names (`postgres`, `mlflow`, `kafka`,
`debezium`, …), the internal ports behind them, and the `mlflow` database created by
`deploy/postgres/init-mlflow.sql` are the stack's own topology. Nothing outside the stack sees
them. The literals that later phases added are in the inventory's per-phase tables, each with
the reason it is not a key.

**Unverified locally:** the compose variables and the connector's `${env:...}` templating have
not been run, because there is no Docker on the development laptop. The YAML and JSON parse,
and the tests check them statically.

## How this is checked

`scripts/audit.py` fails the gate in any of these cases:

1. The tables above do not hold each of A1–A24, B1–B7, C1–C14 and D1–D8 exactly once.
2. A row's status is not `closed…` or `kept…`, or the "Remaining" line is not 0.
3. A path in the "Where" column does not exist.
4. A key is none of these:
   - a `Settings` field;
   - a compose variable listed in `.env.example`;
   - a build argument of the `Dockerfile`.
5. A probe for a baseline literal matches outside `src/oran_adapt/core/config.py`. Examples
   are `claude-sonnet-5`, `oran-adapt-sandbox:latest`, `--pids-limit 128`, `_KILL_GRACE_S =`
   and `DEFAULT_ARTIFACT_MAX_BYTES`. The same applies to the D-row literals in the compose
   file, the connector and the MLflow Dockerfile.
