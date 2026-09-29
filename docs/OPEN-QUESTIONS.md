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
| `job_executor` | `JOB_EXECUTION_MODE` | `process` (hard timeout, isolated) | `thread` (tests, debugging) | Will jobs move to a queue or cluster executor (Celery, Kubernetes Jobs)? |
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

## Not yet behind a port

- **Sandbox backend** (`SANDBOX_BACKEND`, `subprocess` or `docker`): this is a fixed choice in
  the settings schema, not a plugin. The default is `subprocess`, with a soft resident-memory
  watchdog. **The Docker backend is unverified**: nothing has exercised it running.

## Changing a default

Set the selector key in the environment or in the TOML file named by `ORAN_CONFIG_FILE` (see
`config/examples/`). An adapter whose required keys are unset stops startup with a
`ConfigurationError` that names the key. `oran-adapt config lint FILE...` checks a file before
it is deployed.
