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
| `registry` | `REGISTRY_BACKEND` | `mlflow` | none yet | See "Single-adapter ports" below. |
| `auth` | `AUTH_BACKEND` | `api-key` | none yet | Is there an identity provider (OIDC, mTLS) the API must trust? |
| `policy` | `POLICY_BACKEND` | `static-rbac` (roles from `POLICY_ROLES`) | none yet | Does authorization come from an external policy engine (OPA)? |
| `model_handler` | (by framework; every installed handler is used) | `mlflow-flavors` | none yet | Which model formats beyond the MLflow flavors must load? |

## Single-adapter ports

The Unknown-Stack Protocol asks for at least two adapters per port. These ports have one:

- **`registry`**: MLflow with `--serve-artifacts` is a standing project decision, not an unknown,
  and there is no MinIO. The port exists so a second registry can be added without touching
  domain code.
- **`auth`, `policy`**: no identity provider or policy engine has been named. A second adapter
  will be written once the deployment answers the questions in the table above.
- **`model_handler`**: MLflow flavors cover every framework the pipeline trains today.

Each one is still open until a second adapter lands or the question is answered.

## Ports declared without an adapter

`oran_adapt.ports` declares these contracts, but no adapter is registered and nothing resolves
them yet: the code they describe still calls the datastore and the registry directly.
`GET /api/v1/capabilities` lists them with an empty adapter list.

| Port | Today | Scheduled |
|---|---|---|
| `dataset` | `adaptation.data.load_data_version_frame` reads the local datastore | Hardening Phase 5 (data access by reference) |
| `artifact_store` | artifacts go through MLflow `--serve-artifacts` via the registry adapter | Hardening Phase 2 (registry abstraction, filesystem/object-store adapter) |
| `deployment` | "deployed" means the registry's LIVE alias (`LIVE_ALIAS`) | Hardening Phase 3 (deployment and serving propagation) |

## Not yet behind a port

- **Sandbox backend** (`SANDBOX_BACKEND`, `subprocess` or `docker`): this is a fixed choice in
  the settings schema, not a plugin. The default is `subprocess`, with a soft resident-memory
  watchdog. **The Docker backend is unverified**: nothing has exercised it running.

## Changing a default

Set the selector key in the environment or in the TOML file named by `ORAN_CONFIG_FILE` (see
`config/examples/`). An adapter whose required keys are unset stops startup with a
`ConfigurationError` that names the key. `oran-adapt config lint FILE...` checks a file before
it is deployed.
