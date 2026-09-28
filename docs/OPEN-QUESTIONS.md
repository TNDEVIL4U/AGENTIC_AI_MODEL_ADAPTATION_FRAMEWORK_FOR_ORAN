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
| `notification` | `NOTIFICATION_BACKEND` | `log` | `webhook` | Which channel does the operations team watch? |
| `secrets` | `SECRETS_BACKEND` | `env` | `file` (mounted secret files, e.g. Kubernetes/Docker secrets) | Is a secrets manager (Vault, a cloud KMS) mandated? |
| `registry` | `REGISTRY_BACKEND` | `mlflow` (standing decision: MLflow with `--serve-artifacts`) | `filesystem`, `mirror`, `sagemaker`, `vertex` | Will the deployment keep MLflow, or register models in a cloud registry (SageMaker, Vertex)? See "Registry adapters" below. |
| `artifact_store` | `ARTIFACT_STORE_BACKEND` (used by the `filesystem` registry only) | `filesystem` (`ARTIFACT_STORE_ROOT`) | `fsspec` (`ARTIFACT_STORE_URL`: `s3://`, `gs://`, `az://`, ... with the matching fsspec driver) | Is there a shared volume, or must artifacts go to an object store? |
| `auth` | `AUTH_BACKEND` | `api-key` | none yet | Is there an identity provider (OIDC, mTLS) the API must trust? |
| `policy` | `POLICY_BACKEND` | `static-rbac` (roles from `POLICY_ROLES`) | none yet | Does authorization come from an external policy engine (OPA)? |
| `model_handler` | `MODEL_FORMAT` (saving; loading uses whichever installed handler recognises the artifact) | `mlflow-flavors` | `native` (skops / UBJSON / torch files with a manifest; no MLflow needed) | Must serving read MLflow's model format, or the frameworks' own files? |

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
| `dataset` | `adaptation.data.load_data_version_frame` reads the local datastore | Hardening Phase 5 (data access by reference) |
| `deployment` | "deployed" means the registry's LIVE alias (`LIVE_ALIAS`) | Hardening Phase 3 (deployment and serving propagation) |

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

## Not yet behind a port

- **Sandbox backend** (`SANDBOX_BACKEND`, `subprocess` or `docker`): this is a fixed choice in
  the settings schema, not a plugin. The default is `subprocess`, with a soft resident-memory
  watchdog. **The Docker backend is unverified**: nothing has exercised it running.

## Changing a default

Set the selector key in the environment or in the TOML file named by `ORAN_CONFIG_FILE` (see
`config/examples/`). An adapter whose required keys are unset stops startup with a
`ConfigurationError` that names the key. `oran-adapt config lint FILE...` checks a file before
it is deployed.
