# Operations

Running oran-adapt in production. Start with the deployment shape, then configuration, then
the pages for the day-to-day tasks.

## Deploy

| Task | Page |
|---|---|
| Choose a packaging: compose (single host), Helm, or kustomize | [../DEPLOYMENT.md](../DEPLOYMENT.md) |
| Build and verify the `api`, `worker` and `migrator` images, and their SBOMs | [images.md](images.md) |
| Install, upgrade and roll back with the Helm chart (`deploy/helm/oran-adapt`) | [helm.md](helm.md) |
| The same without Helm (`deploy/kustomize`) | [kustomize.md](kustomize.md) |
| Apply schema migrations (expand-only, one-shot migrator; `oran-adapt db upgrade / status / wait`) | [migrations.md](migrations.md) |
| Move from the pilot | [../migration-guide.md](../migration-guide.md) |

## Configure

- One `Settings` schema, read in this order:
  1. the environment;
  2. `.env`;
  3. the TOML file named by `ORAN_CONFIG_FILE`;
  4. the secrets backend.

  Start from `config/examples/` for your stack; [../integration-guide.md](../integration-guide.md)
  explains each example.
- `oran-adapt config lint FILE...` validates a file before it ships: unknown keys, types,
  required keys of the selected adapters, feature checks, production keys, and no secrets in
  files. Run it in CI on every configuration change.
- `oran-adapt config effective`, or `GET /api/v1/config/effective` (admin), shows every key's
  value (secrets redacted) and where it came from.
- Why each default was chosen, and when to change it: [../adr/README.md](../adr/README.md) and
  [../OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md).
- Security: identity backends, the authorization matrix, secrets, outbound policy and rate
  limits are in [../security.md](../security.md) and
  [../security/authz-matrix.md](../security/authz-matrix.md). Make keys with
  `oran-adapt auth new-key`.

## Run

| Task | How |
|---|---|
| Health and readiness | `GET /api/v1/health` (liveness), `GET /api/v1/ready` (readiness: each component answers); workers: `oran-adapt worker health` |
| See and control jobs | `oran-adapt jobs list / cancel / reap`, `GET /api/v1/adaptation/jobs` |
| See and control rollouts | `oran-adapt rollout list / approve / reject`, `POST /api/v1/rollouts/{id}/approve` or `/reject`; [../adapters/rollout_metrics.md](../adapters/rollout_metrics.md) |
| Roll a model back | `POST /api/v1/models/{id}/rollback` |
| Replay notifications | `oran-adapt notifications dispatch`; [../adapters/notification.md](../adapters/notification.md) |
| Change data capture | [../CDC.md](../CDC.md) |

## Observe

- Metrics, traces, logs, the dashboard and the 12 alert rules are described in
  [observability.md](observability.md).
- Each alert has a runbook: what it means, how to confirm it, what to do.

| Alert | Runbook |
|---|---|
| OranAdaptApiDown | [../runbooks/OranAdaptApiDown.md](../runbooks/OranAdaptApiDown.md) |
| OranAdaptNoWorker | [../runbooks/OranAdaptNoWorker.md](../runbooks/OranAdaptNoWorker.md) |
| OranAdaptJobQueueStalled | [../runbooks/OranAdaptJobQueueStalled.md](../runbooks/OranAdaptJobQueueStalled.md) |
| OranAdaptJobFailureRate | [../runbooks/OranAdaptJobFailureRate.md](../runbooks/OranAdaptJobFailureRate.md) |
| OranAdaptJobQuarantined | [../runbooks/OranAdaptJobQuarantined.md](../runbooks/OranAdaptJobQuarantined.md) |
| OranAdaptStageSlow | [../runbooks/OranAdaptStageSlow.md](../runbooks/OranAdaptStageSlow.md) |
| OranAdaptAdapterErrors | [../runbooks/OranAdaptAdapterErrors.md](../runbooks/OranAdaptAdapterErrors.md) |
| OranAdaptAdapterSlow | [../runbooks/OranAdaptAdapterSlow.md](../runbooks/OranAdaptAdapterSlow.md) |
| OranAdaptDeliveryFailures | [../runbooks/OranAdaptDeliveryFailures.md](../runbooks/OranAdaptDeliveryFailures.md) |
| OranAdaptRolloutRolledBack | [../runbooks/OranAdaptRolloutRolledBack.md](../runbooks/OranAdaptRolloutRolledBack.md) |
| OranAdaptNotificationDeadLetters | [../runbooks/OranAdaptNotificationDeadLetters.md](../runbooks/OranAdaptNotificationDeadLetters.md) |
| OranAdaptLlmBudgetExhausted | [../runbooks/OranAdaptLlmBudgetExhausted.md](../runbooks/OranAdaptLlmBudgetExhausted.md) |

## Know the limits

[../LIMITATIONS.md](../LIMITATIONS.md) lists what the framework does not do, and what has not
been verified outside the development machine. Read it before promising any of those things
to a site.
