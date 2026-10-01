# Architecture decision records

Decisions that shape the framework, and one record per Unknown-Stack default: where the target
stack is unknown the framework ships a port, at least two adapters and an extension path, and
picks a default. Each default record names its selector key and value;
`tests/unit/test_phase14_integration.py::test_every_unknown_stack_default_has_an_adr` fails
when a selector has no record or the recorded default differs from `Settings`.

Format: context, decision, consequences, and when to revisit. Superseding a decision means a new
record that says which one it replaces; records are not rewritten.

## Architecture

| ADR | Decision |
|---|---|
| [0001-ports-and-adapters.md](0001-ports-and-adapters.md) | Ports and adapters for every unknown stack choice |
| [0002-configuration.md](0002-configuration.md) | Configuration: environment first, TOML files, lint before deploy |
| [0003-declarative-drift-mappers.md](0003-declarative-drift-mappers.md) | Monitoring payloads are mapped to DriftEvents by mapping files |
| [0004-escape-hatches.md](0004-escape-hatches.md) | Webhook and GitOps as the escape hatch for any serving system |

## Unknown-Stack defaults

| ADR | Selector | Default | Alternatives shipped |
|---|---|---|---|
| [0101-default-registry-backend.md](0101-default-registry-backend.md) | `REGISTRY_BACKEND` | `mlflow` | `filesystem`, `mirror`, `sagemaker`, `vertex` |
| [0102-default-deployment-backend.md](0102-default-deployment-backend.md) | `DEPLOYMENT_BACKEND` | `registry-alias` | `bentoml`, `gitops`, `k8s`, `kserve`, `sagemaker`, `seldon`, `triton`, `vertex`, `webhook` |
| [0103-default-model-format.md](0103-default-model-format.md) | `MODEL_FORMAT` | `mlflow-flavors` | `native` |
| [0104-default-artifact-store-backend.md](0104-default-artifact-store-backend.md) | `ARTIFACT_STORE_BACKEND` | `filesystem` | `fsspec` |
| [0105-default-llm-provider.md](0105-default-llm-provider.md) | `LLM_PROVIDER` | `none` | `anthropic`, `gemini`, `openai-compatible` |
| [0106-default-job-execution-mode.md](0106-default-job-execution-mode.md) | `JOB_EXECUTION_MODE` | `process` | `thread` |
| [0107-default-job-queue-backend.md](0107-default-job-queue-backend.md) | `JOB_QUEUE_BACKEND` | `database` | `celery`, `inline`, `kubernetes`, `rq` |
| [0108-default-cdc-mode.md](0108-default-cdc-mode.md) | `CDC_MODE` | `disabled` | `kafka`, `polling` |
| [0109-default-auth-backend.md](0109-default-auth-backend.md) | `AUTH_BACKEND` | `api-key` | `gateway`, `mtls`, `oidc` |
| [0110-default-policy-backend.md](0110-default-policy-backend.md) | `POLICY_BACKEND` | `static-rbac` | `opa` |
| [0111-default-notification-backend.md](0111-default-notification-backend.md) | `NOTIFICATION_BACKEND` | `log` | `email`, `kafka`, `nats`, `pagerduty`, `pubsub`, `slack`, `sns`, `sqs`, `webhook` |
| [0112-default-dataset-backends.md](0112-default-dataset-backends.md) | `DATASET_BACKENDS` | `none` | `file`, `fsspec`, `gcs`, `http`, `s3` |
| [0113-default-secrets-backend.md](0113-default-secrets-backend.md) | `SECRETS_BACKEND` | `env` | `file`, `vault` |
| [0114-default-rollout-metrics-backend.md](0114-default-rollout-metrics-backend.md) | `ROLLOUT_METRICS_BACKEND` | `api` | `prometheus` |
| [0115-default-delivery-strategy.md](0115-default-delivery-strategy.md) | `DELIVERY_STRATEGY` | `shadow` | `canary`, `blue_green`, `ab`, `manual` |
