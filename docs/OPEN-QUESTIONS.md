# Open questions: stack choices the deployment has not fixed

Where the target deployment's stack is unknown, the code depends on a port
(`oran_adapt.ports`) and ships adapters behind it. The configuration picks one adapter with the
selector key listed below. This file records the default each selector falls back to and the
question that would settle it for good.

The live list comes from `GET /api/v1/capabilities` or `oran-adapt config effective`. This file
is the record of *why* each default was chosen.

| Port | Selector key | Default | Other adapters | Open question |
|---|---|---|---|---|
| `llm` | `LLM_PROVIDER` | `none` (deterministic path, no LLM) | `anthropic`, `gemini` | Which LLM provider, if any, is the operator allowed to call from the RIC environment? |
| `cdc_source` | `CDC_MODE` | `disabled` | `polling` (DB trigger + outbox table), `kafka` | Is a Kafka/Debezium pipeline available, or only the database itself? |
| `job_executor` | `JOB_EXECUTION_MODE` (how a worker runs one attempt) | `process` (hard timeout, whole process tree killed) | `thread` (tests, debugging) | – (settled: jobs run in workers since Phase 6) |
| `job_queue` | `JOB_QUEUE_BACKEND` | `database` (workers poll the job table; no broker) | `celery`, `rq`, `kubernetes`, `inline` (development only) | Is there a broker (RabbitMQ, Redis) or a cluster the workers should be woken through? See "Job queue adapters" below. |
| `notification` | `NOTIFICATION_BACKEND` (comma-separated: several sinks at once, or `none`) | `log` | `webhook`, `slack`, `pagerduty`, `email`, `kafka`, `sqs`, `sns`, `pubsub`, `nats` | Which channel does the operations team watch, and which system consumes job events? See "Notification adapters" below. |
| `secrets` | `SECRETS_BACKEND` | `env` | `file` (mounted secret files, e.g. Kubernetes/Docker secrets) | Is a secrets manager (Vault, a cloud KMS) mandated? |
| `registry` | `REGISTRY_BACKEND` | `mlflow` (standing decision: MLflow with `--serve-artifacts`) | `filesystem`, `mirror`, `sagemaker`, `vertex` | Will the deployment keep MLflow, or register models in a cloud registry (SageMaker, Vertex)? See "Registry adapters" below. |
| `artifact_store` | `ARTIFACT_STORE_BACKEND` (used by the `filesystem` registry only) | `filesystem` (`ARTIFACT_STORE_ROOT`) | `fsspec` (`ARTIFACT_STORE_URL`: `s3://`, `gs://`, `az://`, ... with the matching fsspec driver) | Is there a shared volume, or must artifacts go to an object store? |
| `auth` | `AUTH_BACKEND` | `api-key` | none yet | Is there an identity provider (OIDC, mTLS) the API must trust? |
| `policy` | `POLICY_BACKEND` | `static-rbac` (roles from `POLICY_ROLES`) | none yet | Does authorization come from an external policy engine (OPA)? |
| `model_handler` | `MODEL_FORMAT` (saving; loading uses whichever installed handler recognises the artifact) | `mlflow-flavors` | `native` (skops / UBJSON / torch files with a manifest; no MLflow needed) | Must serving read MLflow's model format, or the frameworks' own files? |
| `deployment` | `DEPLOYMENT_BACKEND` | `registry-alias` (serving loads `model://<name>@<LIVE_ALIAS>`, the pre-Phase-3 behaviour) | `webhook`, `bentoml`, `gitops`, `triton`, `kserve`, `seldon`, `k8s`, `sagemaker`, `vertex` | What serves models in the RIC: a model server (Triton, BentoML), Kubernetes (KServe, Seldon, plain Deployments), GitOps, or a cloud endpoint? See "Deployment adapters" below. |

## Single-adapter ports

The Unknown-Stack Protocol asks for at least two adapters per port. These ports have one:

- **`auth`, `policy`**: no identity provider or policy engine has been named. A second adapter
  will be written once the deployment answers the questions in the table above.

Each one is still open until a second adapter lands or the question is answered.

## Ports declared without an adapter

`oran_adapt.ports` declares these contracts, but no adapter is registered and nothing resolves
them yet: the code they describe still calls the datastore and the registry directly.
`GET /api/v1/capabilities` lists them with an empty adapter list.

| Port | Today | Scheduled |
|---|---|---|
| – | – | – |

`deployment` left this list in Phase 3 and now has ten adapters; `dataset` left it in Phase 5
with five (see "Dataset adapters" below).

## Registry adapters

Phase 2 put every registry behind `ModelRegistryPort`; `oran_adapt.conformance.registry` is the
behaviour each adapter must show, and `docs/adapters/registry.md` explains how to add one. A
model is addressed as `model://<name>/<version>` or `model://<name>@<alias>`
(`oran_adapt.core.model_uri`). The registry stores and returns artifact directories; building
and loading a model object is the model handler's job.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| MLflow stays the default registry. | Standing decision; nothing named another registry. | `REGISTRY_BACKEND` |
| The filesystem registry's root is a volume every replica mounts. A lock left by a crashed writer (`models/<name>/.lock`) is reported after the timeout and must be removed by hand. | No coordination service is assumed. | `REGISTRY_FS_ROOT`, `REGISTRY_FS_LOCK_TIMEOUT_S` |
| A mirror replica's versions are mapped to the primary's by the version tag `oran.mirror.source_version`, since the two backends number versions independently. A failed replica write fails the call by default; `sync` repairs the replica. | Divergence must be visible, not silent. | `REGISTRY_MIRROR_PRIMARY`, `REGISTRY_MIRROR_REPLICA`, `REGISTRY_MIRROR_ON_REPLICA_ERROR` |
| SageMaker: a model is a model package group whose name is the model name with `_`/`.` turned into `-` (hash-suffixed when changed or too long); the original name is the group tag `oran:model-name`, so two names never share a group. Aliases are group tags `oran-alias:<alias>`. Version tags are customer metadata properties, so a value holds at most 256 characters and a version at most 50 keys (the pipeline's `adaptation.note` is cut to 250). New packages are `PendingManualApproval`. | SageMaker's naming and metadata limits. | `SAGEMAKER_GROUP_PREFIX`, `SAGEMAKER_INFERENCE_IMAGE`, `SAGEMAKER_CONTENT_TYPES` |
| Vertex: tags, metrics and lineage are JSON files beside the artifact in GCS (labels cannot hold checksums or free text); tag writes are generation-matched and retried. `default` is Vertex's own alias and is neither shown nor settable; aliases must match Vertex's stricter syntax. Moving an alias is remove-then-add, so a reader can briefly see it unset. | Vertex label and alias rules. | `VERTEX_GCS_BUCKET`, `VERTEX_GCS_PREFIX`, `VERTEX_TAG_UPDATE_ATTEMPTS` |
| SageMaker and Vertex were tested only against the API emulators in `tests/unit/registry_emulators.py`. **They are unverified against AWS and GCP**; the live conformance run is a heavy test that needs real credentials and configuration. | No cloud account in the local gate. | `SAGEMAKER_*`, `VERTEX_*` |
| SageMaker transient errors (throttling, 5xx) are retried by botocore's own defaults. No key sets the retry mode or attempt count yet. | Not yet exposed; recorded as remaining hardcoding in the Phase 2 report. | none yet |
| `ENVIRONMENT=production` requires `DATABASE_URL`, `ARTIFACT_WORKDIR` and the selected adapters' `production_keys`: `MLFLOW_TRACKING_URI` for `mlflow`; `REGISTRY_FS_ROOT` and `ARTIFACT_STORE_ROOT` for `filesystem` on the filesystem store. A `mirror`'s primary and replica are not expanded: set their storage keys explicitly. | Production must not write to a development path by default. | `ENVIRONMENT` |
| An adapter shipped outside this repository cannot add fields to the core `Settings`. It reads its own prefixed keys with its own settings class, validated when it is built at startup (see `templates/registry-adapter`), so the core's config lint does not see them. | `Settings` is a closed, typed schema. | the adapter's own env prefix |
| A `native` torch artifact is a pickle (`torch.save` of the module), so loading it runs code from the artifact; load only artifacts from a registry you trust. | torch has no safe whole-module format. | `MODEL_FORMAT` |

## Deployment adapters

Phase 3 put serving behind `DeploymentPort`. Every promotion and rollback rolls the version out
and reads it back from the serving system (`registry.deployment.Deployer`); when that fails, the
previous version is restored in the registry alias and in the serving system alike.
`oran_adapt.conformance.deployment` is the behaviour each adapter must show, and
`docs/adapters/deployment.md` explains the rules, the webhook contract and each adapter's keys.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| The default stays `registry-alias` on `LIVE_ALIAS`: "deployed" means "live", as before Phase 3. | No serving system has been named; existing installations must not change behaviour. | `DEPLOYMENT_BACKEND`, `DEPLOYMENT_ALIAS` |
| A rollout must read back within 10 minutes, polled every 5 s; then the previous version is restored. | Cold starts on Kubernetes and cloud endpoints take minutes; a model server load takes seconds. | `DEPLOYMENT_TIMEOUT_S`, `DEPLOYMENT_POLL_S` |
| A serving system that settles at a version other than the one asked for (for example SageMaker's own automatic rollback) counts as a failed rollout. | Only the requested version counts as deployed. | none |
| If the restore after a failed rollout does not read back either, the error says so (`restored: false`) and nothing retries on its own. | A second automatic move could make things worse; an operator decides. | none |
| `gitops` commits only the manifest path in a checkout owned by the adapter, and reads status from the serving system (`GITOPS_STATUS_URL`), never from git. | A commit is not a deployment. | `GITOPS_*` |
| `triton` stages artifacts into a repository directory the server also sees (shared volume), and runs in explicit model-control mode. The framework does not convert models to a Triton layout. | Triton loads only from its model repository. | `TRITON_REPOSITORY`, `TRITON_BASE_CONFIG` |
| The Kubernetes adapters use plain REST with a bearer token (no Kubernetes SDK) and trust readiness only once `observedGeneration` has caught up. | Keeps the dependency surface small; stale status must not pass as the new version. | `K8S_*`, `KSERVE_*`, `SELDON_*` |
| `sagemaker` and `vertex` deploy only versions of their own cloud registry, so they require `REGISTRY_BACKEND` (or a mirror's primary) to match. | The endpoint needs a model package or Vertex model to serve. | `REGISTRY_BACKEND` |
| `webhook`, `bentoml` and `triton` were tested only against the stdlib stub in `tests/unit/serving_stub.py`; `gitops` against a real git repository and a controller emulator; `kserve`, `seldon`, `k8s`, `sagemaker` and `vertex` only against the API emulators in `tests/unit/deployment_emulators.py`. **None is verified against the real system**; `templates/bentoml-service` has never run under BentoML. The live conformance run is a heavy test that needs a real endpoint. | No cluster, cloud account or model server in the local gate. | the adapter's keys |

## Notification adapters

Phase 4 made every job state transition an event in a durable outbox, written in the
transition's own transaction, and delivered at least once to each sink `NOTIFICATION_BACKEND`
names: signed (Standard Webhooks HMAC, with key rotation), retried with backoff, behind a
per-sink circuit breaker, and dead-lettered for `GET /api/v1/deliveries` and redrive.
`oran_adapt.conformance.notification` is the behaviour each sink must show, and
`docs/adapters/notification.md` explains the envelope, signature verification and each
adapter's keys.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| The default sink stays `log`: events are written and logged, nothing leaves the host. | No channel has been named; existing installations must not start calling out. | `NOTIFICATION_BACKEND` |
| The dispatcher runs inside the API process. | One process to operate; a separate `oran-adapt notifications dispatch` process is supported for larger installations. | `NOTIFICATION_DISPATCH_ENABLED` |
| Delivery is at least once, never exactly once; receivers deduplicate on the event id (`webhook-id`). | Exactly once is impossible across a crash between "sent" and "recorded". | none |
| 8 attempts, backoff 2 s doubling to 10 min with +/-20% jitter, then dead-letter. | About half an hour of retries rides out a receiver restart without flooding it. | `NOTIFICATION_MAX_ATTEMPTS`, `NOTIFICATION_BACKOFF_*` |
| 5 consecutive failures open a sink's circuit for 60 s. | A dead sink must not burn every delivery's attempts, nor slow the other sinks. | `NOTIFICATION_BREAKER_*` |
| A claimed delivery is re-claimable after 60 s. | Longer than the 10 s sink timeout, so a slow send is never sent twice concurrently. | `NOTIFICATION_LEASE_S`, `NOTIFICATION_TIMEOUT_S` |
| PagerDuty gets only failures, timeouts and rollbacks; every other sink gets every transition. | Paging on each intermediate state would train people to ignore pages. | `NOTIFICATION_SINK_EVENTS` |
| Production refuses a `webhook` sink without signing keys; other sinks rely on their own authentication (routing keys, broker credentials, IAM). | An unsigned webhook can be forged by anyone who finds the URL. | `NOTIFICATION_SIGNING_KEYS` |
| Only job state transitions are events. Promotions and manual rollbacks (`POST /models/{id}/rollback`) outside a job are not yet. | The finding asked for job transitions; model-level events need their own type names. | none (a later phase) |
| `webhook`, `slack`, `pagerduty` and `pubsub` were tested only against a local HTTP receiver, `nats` against a local server speaking the NATS protocol, and `email`, `kafka`, `sqs` and `sns` against client doubles in `tests/unit/notification_doubles.py`. **None is verified against the real service.** | No broker, cloud account, Slack workspace or PagerDuty service in the local gate. | the adapter's keys |

## Dataset adapters

Phase 5 lets a data version be a reference to a Parquet, CSV or JSON Lines object read in
batches by a `DatasetPort` adapter (`file`, `http`, `fsspec`, `s3`, `gcs`) instead of rows
posted inline. `oran_adapt.conformance.dataset` is the behaviour each adapter must show, and
`docs/adapters/dataset.md` explains each key.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| No adapter is enabled by default: data is sent inline, as before. | Reading from a store is a decision per installation, and every adapter needs an allow-list. | `DATASET_BACKENDS` |
| Every adapter reads only inside an explicit allow-list (directories, hosts, prefixes, buckets); an empty list refuses everything. | A URI in a request body must not become a way to read arbitrary files or reach internal hosts. | `DATASET_FILE_ROOTS`, `DATASET_HTTP_ALLOWED_HOSTS`, `DATASET_FSSPEC_PREFIXES`, `DATASET_S3_BUCKETS`, `DATASET_GCS_BUCKETS` |
| A registered object must not change. It is checked by fingerprint before each read; a change is refused, never silently re-read. File and fsspec fingerprints (size + modification time) can miss a same-size rewrite within the clock's resolution. | The content hash of a version is its identity for lineage and reproducibility. | `DATASET_VERIFY_ON_READ` (`hash` re-hashes every read) |
| S3 pins `versionId` only on versioned buckets; on an unversioned bucket an overwrite is detected by ETag and refused. | Only a versioned store can serve the registered bytes after an overwrite. | bucket versioning |
| A job holds at most 1 000 000 rows; drift analysis samples at most 100 000 rows per version; objects over 4 GiB are refused. | The framework runs on small machines; limits are checked before reading. | `DATASET_MAX_ROWS`, `DATASET_ANALYSIS_MAX_ROWS`, `DATASET_MAX_SOURCE_BYTES` |
| Referenced data needs a timestamp column; there is no "start + spacing" fallback. | Row order in an object is not a time axis. | `DATASET_TIME_COLUMN`, `timestamp_column` per version |
| CDC can follow any table; `kpi_sample` is only the default. Triggers are generated for review, not applied automatically. | Applying DDL to an operator's database is the operator's decision. | `CDC_*_COLUMN`, `CDC_DATASET_ID`, `oran-adapt cdc trigger-sql` |
| `file` and `fsspec` (`memory://`) were tested for real, `http` and `gcs` against a local server, `s3` against a client double. **None of `http`, `s3`, `gcs` or a non-memory fsspec filesystem is verified against a real store.** | No cloud account or object store in the local gate. | the adapter's keys |

## Job queue adapters

Since Phase 6 the API only queues a job; a worker process (`oran-adapt worker run`) claims and
runs it. The database is the queue: a claim is one conditional `UPDATE`, and a broker adapter
only wakes workers, so a lost or repeated broker message neither loses nor doubles a job.
`oran_adapt.conformance.job_queue` is the behaviour each adapter must show, and
`docs/adapters/job_queue.md` explains each key.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| Workers poll the database; no broker is required. | Every installation already has the database; a broker is a decision per installation. | `JOB_QUEUE_BACKEND`, `JOB_POLL_INTERVAL_S` |
| A worker renews its lease every 5 s; a lease unrenewed for 30 s is taken back by the reaper. | Detects a dead worker within about half a minute without much database traffic. | `JOB_HEARTBEAT_S`, `JOB_LEASE_TTL_S`, `JOB_REAP_INTERVAL_S` |
| A job whose attempts end 3 times without an outcome is quarantined. | A job that kills its workers must not take down the pool. | `JOB_POISON_THRESHOLD` |
| A stopping worker waits 30 s for its job, then requeues it without charging an attempt. | Deploys and scale-downs must not use up retries. | `JOB_DRAIN_TIMEOUT_S` |
| No deadline and no per-tenant limit by default. | Neither has a value the framework can know. | `JOB_DEADLINE_S`, `JOB_TENANT_CONCURRENCY`, `JOB_TENANT_LIMITS` |
| One worker class, `default`; GPU jobs need a class mapping and workers started for it. | No GPU node is known. | `JOB_CLASS_BY_FRAMEWORK`, `JOB_WORKER_CLASSES`, `JOB_QUEUE_K8S_CLASS_PODS` |
| A job lost while registering or promoting is not re-run; it fails `JOB_ABANDONED` with `needs_reconciliation`. | Re-running could register or promote twice. | – |
| `inline` runs a job inside its request, and production refuses it. | Only for demos and one-process development. | `JOB_QUEUE_BACKEND` |
| No `arq` adapter; `rq` covers the Redis family. | An arq adapter would repeat the RQ one through the same port. | – |
| `celery`, `rq` and `kubernetes` were tested against doubles only. **None is verified against a real broker or cluster, and the claim was not run on PostgreSQL.** | No broker, cluster or PostgreSQL in the local gate. | the adapter's keys |

## Validation gate and progressive delivery

Since Phase 7 a candidate replaces the incumbent only through the gate
(`validation/gate.py`, policy `GATE_POLICY`), and reaches traffic through `DELIVERY_STRATEGY`
(`delivery/controller.py`). Rollouts read online metrics through `RolloutMetricsPort`
(`api`, `prometheus`); `oran_adapt.conformance.rollout_metrics` is the behaviour each source must
show, and `docs/adapters/rollout_metrics.md` explains every key.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| Superiority with margin 0 at 95 %: the paired-bootstrap interval of the improvement must lie above zero. An equal or marginally worse candidate is rejected. | Finding 6: a retrained model must be demonstrably better to replace the incumbent. | `GATE_POLICY` (`mode`, `margin`, `confidence`) |
| Calibration, latency (3x with a 10 ms floor) and size (5x with a 1 MiB floor) guardrails on; no slice columns. | Slices are specific to each dataset; the others apply to every model. | `GATE_POLICY` (`slices`, `calibration`, `latency`, `size`) |
| A first version (no incumbent) is judged on its own and accepted when it can be scored. | There is nothing to compare it with. | - |
| `shadow` is the default strategy, then approval. | It needs no traffic split, so it works with every deployment backend, and nobody's traffic changes without a person deciding. | `DELIVERY_STRATEGY`, `DELIVERY_POLICY.shadow_then` |
| Health: `error_rate` at most 0.01 worse than stable, `latency_p95_ms` at most 1.5x (optional), at least 100 requests per arm. | Conservative values for request/response serving; nothing is judged on a handful of requests. | `DELIVERY_POLICY` (`health`, `min_samples`) |
| Canary 5/25/50/100 %, each step held 10 minutes; A/B 50 % for 1 h, Welch t-test at 95 %, inconclusive rolls back; approval waits 24 h; shadow waits 24 h. | Common practice; each is a policy field. | `DELIVERY_POLICY` |
| A canary step that never collects `min_samples` waits indefinitely. | Rolling back for lack of traffic would punish quiet cells; an operator can reject. | - |
| `shadow` and `manual` rollouts that may continue as a canary count as needing a traffic split at startup. | Refusing at startup is better than failing mid-rollout. | `DELIVERY_POLICY.shadow_then`, `approval_then` |
| The `api` source is the default: the serving layer posts observations. | Works without a metrics system. | `ROLLOUT_METRICS_BACKEND` |
| Only `registry-alias`, `webhook` and `kserve` split traffic. `registry-alias` records the split in the canary alias and a tag, which the serving layer must honour. **The splits on real serving systems and the `prometheus` source against a real Prometheus are unverified.** | No serving system or Prometheus in the local gate. | `DEPLOYMENT_CANARY_ALIAS`, `DEPLOYMENT_TRAFFIC_TAG`, `ROLLOUT_PROMETHEUS_*` |

## Model type plugins

Hardening Phase 8 put every model library behind `ModelTypePort` (`oran_adapt.model_type`
entry points): `sklearn`, `xgboost`, `lightgbm`, `catboost`, `torch`, `torch-sequence`, `keras`,
`onnx` and `statsmodels`, plus a template. `oran_adapt.conformance.model_types` is the behaviour
each plugin must show, and `docs/adapters/model_type.md` explains every key.

Defaults and assumptions, with the key that changes each:

| Assumption | Why | Key |
|---|---|---|
| With `MODEL_TYPES` empty, every installed plugin is tried in name order, and the first that serves the framework and accepts the object wins. | Deterministic without configuration; `torch` and `torch-sequence` never both accept a module. | `MODEL_TYPES` |
| `DECISION_SUPPORTED_FRAMEWORKS` empty means every framework an installed plugin can adapt. With none, the decision is NO_ACTION and the typed `UNSUPPORTED_MODEL_TYPE` reason is recorded as the reuse reason. | The list follows what is installed instead of a vendor list in config. | `DECISION_SUPPORTED_FRAMEWORKS` |
| On the hold-out, the first `window - 1` rows of a sequence model are scored on edge-padded windows. | Every held-out row gets a prediction, so the paired gate compares the same rows for both models. Training uses only windows of real rows. | `SEQUENCE_WINDOW` (or the model's `sequence_window`) |
| Sequence training keeps the best epoch on the newest `SEQUENCE_VALIDATION_FRACTION` of training windows; 20 epochs to fine-tune, 200 to retrain, Adam at 0.01. | Small, CPU-friendly budgets; Keras keeps its compiled optimizer. | `SEQUENCE_*` |
| The gate's bootstrap and slice guardrails resample rows independently, which ignores autocorrelation for temporal models. | A block bootstrap is not implemented; the paired comparison still uses the same newest rows. | `GATE_POLICY` (slices off by default) |
| statsmodels forecasters are scored by a forecast from the end of their own sample over the hold-out horizon; fine-tuning is a filter update (`apply(refit=False)`), full retraining re-estimates (`apply(refit=True)`). Holt-Winters and other results without `apply` are unsupported. | State space results carry their own time index; exogenous regressors keep their names. | - |
| ONNX models are scored but never adapted. `ONNX_RUNTIME=onnxruntime` is the default; `reference` uses the `onnx` package's evaluator. | An ONNX graph has no training state. **onnxruntime was not installed locally: only the reference evaluator ran.** | `ONNX_RUNTIME` |
| Candidates of every plugin are written with joblib (Keras models included); the registry handlers store models natively (skops, `onnx.save_model`, statsmodels `save`, `.keras`). | One candidate format for the sandbox and the gate. | - |
| **LightGBM, CatBoost and Keras plugins are verified only against test doubles** that mimic their APIs (`tests/unit/model_type_doubles.py`). | The libraries are not installed on the gate machine. | - |

## Not yet behind a port

- **Sandbox backend** (`SANDBOX_BACKEND`, `subprocess` or `docker`): this is a fixed choice in
  the settings schema, not a plugin. The default is `subprocess`, with a soft resident-memory
  watchdog. **The Docker backend is unverified**: nothing has exercised it running.

## Changing a default

Set the selector key in the environment or in the TOML file named by `ORAN_CONFIG_FILE` (see
`config/examples/`). An adapter whose required keys are unset stops startup with a
`ConfigurationError` that names the key. `oran-adapt config lint FILE...` checks a file before
it is deployed.
