# As-built description (Hardening Phase 0 baseline)

Snapshot of branch `phase14-production-hardening` at commit `2893513`, written by reading the
code, not the older design docs. Where this file and an older doc disagree, this file describes
what the code does. "Hardening Phase N" refers to the production-hardening programme; the repo's
own earlier "Phase 1–15" commit labels are a different, older numbering.

## 1. Process and threading model

- One FastAPI app (`src/oran_adapt/api/app.py`, `create_app`), served by uvicorn
  (`oran_adapt.api.main:app`). Dependencies (DB engine, session factory, `MlflowRegistry`, LLM
  client, settings) hang off `app.state`. There are no module globals apart from the cached
  `get_settings()`.
- **`POST /api/v1/adaptation/events` is synchronous.** The route is a plain `def`, so FastAPI runs
  it on its worker threadpool, and the HTTP request blocks until the job reaches a terminal state
  (`orchestrator/jobs.py:submit_adaptation_job`).
- Each job attempt runs according to `JOB_EXECUTION_MODE`:
  - `process` (default): a fresh `multiprocessing` **spawn** child per attempt
    (`jobs.py:_run_in_process`). On timeout the parent terminates it, then kills it after
    `_KILL_GRACE_S = 5.0` s. On POSIX the child leads a process group, so sandbox children die
    with it. On Windows only the child dies.
  - `thread`: a `ThreadPoolExecutor(max_workers=1)` thread. It cannot be killed on timeout. Meant
    for tests only.
- Retries happen inside the request: only `RegistryUnavailableError` and
  `DatabaseUnavailableError` are retried, `JOB_MAX_RETRIES` times with exponential backoff that
  `time.sleep`s on the request thread.
- Concurrency control: the unique DB constraint on `adaptation_job.idempotency_key`, plus a
  per-model row lock in `model_lock` with a TTL (`MODEL_LOCK_TTL_S`). A second event for a busy
  model is refused with `MODEL_BUSY` (409), and nothing is recorded.
- CDC consumer: a separate long-running CLI process (`oran-adapt cdc run --mode kafka|polling`).
- **Since Hardening Phase 6 this section describes the old model.** The POST only records the job
  as `QUEUED` (201) and a separate worker process (`oran-adapt worker run`, compose service
  `worker`) claims it under a lease and runs it with `JOB_EXECUTION_MODE`. Retries are requeues,
  and timeouts, deadlines and cancellation kill the whole process tree (`docs/adapters/job_queue.md`).
- There is no broker, no work queue, no separate worker service and no outbound notifications.
  Callers poll `GET /jobs/{id}`, or simply wait on the blocking POST. (Since Hardening Phase 4
  every job transition is also delivered to the configured notification sinks through a
  durable outbox: `docs/adapters/notification.md`.)

## 2. HTTP API (prefix `/api/v1`)

Authentication: `X-API-Key` or `Authorization: Bearer <key>`, checked against SHA-256 digests
in `API_KEYS` (`api/security.py`). Every router except health requires a read role; write routes
add stricter roles. With `AUTH_ENABLED=false`, every caller is `anonymous`/ADMIN.
(Since Hardening Phase 9: identity comes from `AUTH_BACKEND` (`api-key`, `oidc`, `gateway`,
`mtls`), every route must declare a policy or the API refuses to start, and the Roles column
is generated in `docs/security/authz-matrix.md`; see `docs/security.md`.)

| Method | Path | Roles | Request | Response |
|---|---|---|---|---|
| GET | `/health` | none | – | `HealthResponse{status, version}` |
| GET | `/ready`, `/readiness` | none | – | `ReadyResponse{ready, components[]}`; 503 unless DB **and** MLflow answer |
| GET | `/metrics` | none if `METRICS_PUBLIC` | – | Prometheus text |
| POST | `/adaptation/events` | ADMIN, OPERATOR, ML_ENGINEER | `DriftEvent` (`core/schemas.py`) | `JobResponse`; 201 new, 200 duplicate (since Phase 6: returned at once with status `QUEUED`) |
| GET | `/adaptation/jobs/{job_id}` | any | – | `JobResponse` + `transitions[]` |
| GET | `/adaptation/jobs?status&model_id&tenant&quarantined&limit&offset` | any | – | jobs, newest first (Phase 6) |
| POST | `/adaptation/jobs/{job_id}/cancel` | ADMIN, OPERATOR, ML_ENGINEER | – | 200 cancelled, 202 cancel requested, 409 `JOB_NOT_CANCELLABLE` (Phase 6) |
| POST | `/datasets` | ADMIN, ML_ENGINEER | `{dataset_id, name?, description?}` | dataset |
| GET | `/datasets` | any | – | list |
| POST | `/datasets/{id}/versions` | ADMIN, ML_ENGINEER | `VersionCreate` with **inline `records: list[dict]`** (since Hardening Phase 5: *or* a `storage_uri` read by a dataset adapter, see `docs/adapters/dataset.md`) | `VersionInfo`; 201/200 |
| GET | `/datasets/{id}/versions`, `/{version}`, `/{version}/lineage` | any | – | version info / lineage |
| GET | `/datasets/{id}/versions/{version}/rows?offset&limit` | any | – | one page of rows, whatever the storage (Phase 5) |
| POST | `/datasets/{id}/versions/{version}/verify` | ADMIN, ML_ENGINEER | – | re-hashes the rows against the recorded hash (Phase 5) |
| POST | `/datasets/{id}/cdc/materialize` | see route | – | CDC data version |
| GET | `/current-data`, `/current-data/{id}` | any | – | CurrentData records |
| GET | `/models`, `/models/{model_id}` | any | – | model metadata (+ live alias/version) |
| POST | `/models/attach` | ADMIN, ML_ENGINEER | onboarding body | onboarded model |
| GET | `/models/{id}/versions`, `/evaluation`, `/promotions` | any | – | versions / evaluations / promotions |
| POST | `/models/{id}/rollback` | ADMIN, OPERATOR | rollback body | promotion record |

Errors are `AdaptationError.to_dict()` → `{code, message, context}`. The status map is in
`api/app.py:_status_for`. Validation errors return 422 `INVALID_REQUEST` without echoing the
input. Request bodies larger than `API_MAX_REQUEST_BYTES` get 413 `REQUEST_TOO_LARGE`.
Unhandled errors return 500 with only the correlation id.

`DriftEvent.idempotency_key()` is `model_id:event_id` when `event_id` is set, otherwise
`model_id:` plus the first 32 hex characters of a SHA-256 over the canonical body.

## 3. Job state machine (as enforced in `core/state_machine.py`)

```
RECEIVED → VALIDATING → DATA_PREPARING ─┬→ EVALUATING_VERSIONS → REUSE_DECISION ─┬→ PROMOTING
                                        │                                        └→ DECISION_PENDING
                                        ├→ DECISION_PENDING → ADAPTING → VALIDATING_CANDIDATE ─┬→ REGISTERING → PROMOTING
                                        └→ COMPLETED (no drift / too little data)             └→ COMPLETED (rejected)
PROMOTING → COMPLETED | ROLLED_BACK
any non-terminal → FAILED | TIMED_OUT
any non-terminal except RECEIVED/VALIDATING → DATA_PREPARING (transient retry)
```

Terminal states: `COMPLETED`, `FAILED`, `ROLLED_BACK`, `TIMED_OUT`. The legacy values
`ANALYZING`, `MODEL_COMPARISON` and `DECISION_MADE` still load but are never entered. Every
transition writes an `adaptation_event` row. There is no `CANCELLED` (added as a
terminal state in Hardening Phase 6) and no `AWAITING_APPROVAL` (a rollout state since
Hardening Phase 7, in the `rollout` table rather than the job).

"Promotion" means moving the MLflow alias `LIVE_ALIAS` (default `live`) to a new version and
tagging `oran.status`. Nothing is pushed to a serving layer (`registry/promotion.py`).

Validation gate (`validation/engine.py`): the primary metric is the **first key** of the
metrics dict. A classifier passes when `candidate >= current - VALIDATION_ACCURACY_TOLERANCE`,
and a regressor when `candidate_rmse <= current_rmse * (1 + VALIDATION_RMSE_TOLERANCE_RATIO)`.
**A candidate that is somewhat worse can therefore pass** (finding 6). There is no statistical
test and no guardrail metrics. (Since Hardening Phase 7 both keys are gone: a versioned
`GATE_POLICY` requires a paired-bootstrap interval of the improvement to clear a margin, with
slice, calibration, latency and size guardrails; every decision is stored in `gate_decision`.
An accepted candidate reaches traffic through `DELIVERY_STRATEGY`, as a shadow, canary, A/B or
manually approved rollout with automatic rollback; see `docs/adapters/rollout_metrics.md`.)

## 4. Database schema (SQLAlchemy `db/models.py`, Alembic `migrations/versions/0001…0006`)

| Table | Key columns |
|---|---|
| `model_metadata` | model_id, mlflow_model_name, model_type, framework, task_type, target_column, extra |
| `dataset_metadata` | dataset_id, name, description, schema |
| `data_version` | dataset_id, version (unique pair), kind, parent_version_id, data_start/end, row_count, content_hash, schema_hash, storage_uri, status, cdc_range, source_tx |
| `data_record` | data_version_id, observed_at, **payload (JSON, one row per record)**, record_key. Since Hardening Phase 5 only for versions with `extra.storage = rows`; `reference` versions keep their rows in the object at `storage_uri`, and `derived` training snapshots name their source versions |
| `model_data_association` | model_id, model_version, data_version_id, role (unique quad) |
| `performance_record` | model_id, model_version, data_version_id, metric_name, value |
| `adaptation_job` | job_id, **idempotency_key (unique)**, model_id, status, strategy, event, result, error, correlation_id |
| `adaptation_event` | job_id, component, from_status, to_status, message, payload |
| `model_promotion` | model_id, kind, from/to_version, status, reason, actor, job_id, idempotency_key, artifact_sha256 |
| `model_lock` | model_id (PK), job_id, acquired_at, expires_at |
| `model_version_evaluation` | job_id, model_id, model_version, is_live, compatible, reusable, metric_name/value, reuse_score |
| `audit_log` | action, component, actor, model_id/version, status, decision, reason, detail, error, correlation_id |
| `current_data` | current_data_id, data_version_id, model_id, job_id, source_versions, schema, content_hash, quality |
| `kpi_sample` | dataset_id, observed_at, payload: **the only CDC source table** |
| `cdc_changelog`, `cdc_event`, `cdc_offset` | trigger changelog, normalised CDC events, consumer offsets |

Since Hardening Phase 7, migration 0009 adds `gate_decision` (every gate verdict with its
evidence and policy hash), `rollout` (strategy, state, arms, policy, history) and
`rollout_observation` (online metrics posted per arm).

Migration 0005 creates the `kpi_sample` triggers (SQLite and PostgreSQL variants). 0006 sets
`REPLICA IDENTITY FULL` on PostgreSQL. Each migration has a `downgrade()`. The only test of the
downgrades (`tests/unit/test_phase1_foundation.py`) runs a full upgrade to head and a downgrade to
base, on SQLite only.

## 5. Configuration surface

A single `pydantic-settings` class, `core/config.py:Settings` (about 60 keys). Sources are field
defaults, then `.env`, then environment variables; there is no YAML/TOML layer and no secret
references (since Hardening Phase 9, secret-typed settings can come from `SECRETS_BACKEND`:
`env`, `file` or `vault`). Validators check that the LLM key is present for the chosen provider (since Hardening
Phase 10, only when `LLM_ENABLED=true`, which also requires a provider) and that
`API_KEYS` is well formed. There is no effective-config endpoint and no `config-lint` CLI.
`.env.example` lists every key with its default.

## 6. External dependencies

| Dependency | Used for | Where |
|---|---|---|
| PostgreSQL (SQLite for dev/tests) | all state | `db/` |
| MLflow tracking + model registry | model versions, aliases, tags, artifacts, **model loading** (`mlflow.sklearn/xgboost/pytorch`) | `registry/client.py`, `adaptation/loaders.py` |
| MinIO (S3) | MLflow artifact store (compose only). **Removed in Hardening Phase 0**: its public images are gone; MLflow now keeps artifacts on the `mlartifacts` volume | `docker-compose.yml` |
| Kafka + Debezium | CDC from `kpi_sample` (optional) | `cdc/sources.py`, `deploy/debezium/` |
| Evidently AI | KS/PSI drift statistics, validation scores | `analysis/comparison.py`, `validation/evaluate.py` |
| scikit-learn, XGBoost, PyTorch | model inspection, fine-tune and retrain. **Since Hardening Phase 8** every model library sits behind a model type plugin (`ModelTypePort`, `adapters/model_types/`: sklearn, XGBoost, LightGBM, CatBoost, torch tabular, torch sequence, Keras, ONNX, statsmodels), imported lazily; see `docs/adapters/model_type.md` | `adaptation/` |
| Anthropic / Google GenAI | optional LLM strategy choice and generated adapters. **Since Hardening Phase 10** off unless `LLM_ENABLED=true`; providers are adapters (`adapters/llm_providers.py`: `anthropic`, `gemini`, `openai-compatible`), wrapped in the guard (`llm/guard.py`: caps, budget, breaker, retries, `llm_usage` ledger), prompts versioned (`llm/prompts.py`); see `docs/adapters/llm.md` | `llm/client.py`, `decision/llm_selector.py`, `adaptation/llm_adapter.py` |
| Docker CLI | optional sandbox backend | `sandbox/runner.py` |
| Prometheus | scrapes `/metrics` (compose) | `deploy/prometheus/` |

## 7. Packaging state

- `Dockerfile` (api / cdc-consumer image, non-root uid 10001), `docker/mlflow/Dockerfile`,
  `docker/sandbox/Dockerfile`, and `docker-compose.yml` with postgres, minio, minio-init (both removed in Hardening Phase 0), mlflow,
  kafka, debezium, debezium-init, api, cdc-consumer and prometheus.
- **None of these images or the compose stack has ever been built or run.** Docker is not
  installed on the development laptop. Hardening Phase 0 moves the build and bring-up to GitHub
  Actions (`.github/workflows/ci.yml`, job `phase-0-baseline`, running `scripts/ci/compose_smoke.sh`).
- `requirements.lock` was generated on Windows. The Linux image build applies it as a
  constraints file.
- There was no CI before Hardening Phase 0.
- **Since Hardening Phase 11:** the `Dockerfile` has three targets (`api`, `worker`,
  `migrator`), its base is pinned by digest, and compose runs a one-shot `migrate` service and a
  separate `worker`; the API no longer applies migrations. Kubernetes packaging: the Helm chart
  `deploy/helm/oran-adapt` and the kustomize tree `deploy/kustomize` (`docs/operations/`). Both
  are checked statically in the gate and rendered only in CI; nothing has been deployed from
  the development laptop.
- **Since Hardening Phase 12:** `adaptation_job` has a `trace_context` column (migration 0011,
  the intake span's W3C traceparent) and the API, workers and CLI configure OpenTelemetry
  tracing (`TRACING_EXPORTER`, off by default). Workers serve their own `/metrics` on
  `WORKER_METRICS_PORT`. Log lines carry `trace_id`/`span_id` and are redacted. Dashboards live
  in `deploy/helm/oran-adapt/files/dashboards/`, and each of the 12 alerts has a runbook
  (`docs/operations/observability.md`).
