# Migrating from the pilot

The pilot is the Phase 0 baseline (commit `2893513`). This guide takes a pilot installation to
the hardened framework. Most pilot settings keep their names and defaults. The changes below
alter what an operator runs, what a client sees, or what a key means. Work through them in
order, then run the checklist at the end.

The REST contract changed only additively. Every pilot route, field and status code is still
served, and idempotency by `event_id` is kept. The one behavioural change a client sees is
step 2: jobs are now asynchronous.

## 1. Schema migrations are a separate step

- **Pilot:** the API created and upgraded its own tables at startup.
- **Now:** the API never changes the schema. A one-shot migrator does:
  `oran-adapt db upgrade`, which is the `migrate` service in compose, the migrator image, or
  the Helm pre-upgrade job. `oran-adapt db status` shows the revision. `oran-adapt db wait`
  blocks until the schema is current, and the API and worker images run it before they start.
- Migrations `0001` to `0011` are **expand-only**: they add tables and columns and never drop
  or rename them. A pilot database upgrades in place, and the pilot's code keeps working
  against the upgraded schema during a rolling change.
- Back up the database first anyway. See [operations/migrations.md](operations/migrations.md).

## 2. Jobs run in workers

- **Pilot:** `POST /api/v1/adaptation/events` ran the whole pipeline inside the request and
  answered when the job was finished.
- **Now:** the request validates and queues the job, and answers `201` with the job in
  `QUEUED`. A duplicate `event_id` still answers `200` with the existing job.
  - Workers run the jobs: `oran-adapt worker run`, or the `worker` service or deployment.
  - **Without a worker, jobs stay `QUEUED`.** The `OranAdaptNoWorker` alert fires.
  - Clients that read the result from the POST response must now poll
    `GET /api/v1/adaptation/jobs/{job_id}` until a terminal status: `COMPLETED`, `FAILED`,
    `ROLLED_BACK`, `TIMED_OUT` or `CANCELLED`. `CANCELLED` is new (`oran-adapt jobs cancel`).
    The result is the job's `outcome`.
- `JOB_QUEUE_BACKEND=database` (the default) needs nothing beyond the existing database.
  `JOB_EXECUTION_MODE=process` (the default, as in the pilot) runs each job in a child
  process. See [adapters/job_queue.md](adapters/job_queue.md) for the other queues.

## 3. The validation gate is a policy

- **Pilot:** `VALIDATION_ACCURACY_TOLERANCE` (0.02) and `VALIDATION_RMSE_TOLERANCE_RATIO`
  (0.05) compared point estimates.
- **Now:** both keys are gone. `GATE_POLICY` (inline JSON) or `GATE_POLICY_FILE` (TOML)
  defines a versioned policy: a primary metric compared by a paired bootstrap confidence
  interval, plus guardrails. See `config/policies/gate.toml`.
- **The default is stricter than the pilot.** `mode = "superiority"` with `margin = 0`
  accepts a candidate only if it is shown to be better. The closest equivalents of the pilot
  tolerances are:

  ```sh
  # classification: accuracy may drop by at most 0.02
  GATE_POLICY={"mode": "non_inferiority", "margin": 0.02}
  # regression: RMSE may rise by at most 5 % of the incumbent's
  GATE_POLICY={"mode": "non_inferiority", "margin": 0.05, "margin_relative": true}
  ```

  These are equivalent in spirit, not identical: a non-inferiority interval is stricter than
  a point comparison on a small holdout. Every gate decision records the policy version it
  used.
- `VALIDATION_MIN_ROWS` and `VALIDATION_HOLDOUT_FRACTION` are unchanged.

## 4. Promotion is a delivery

- **Pilot:** an accepted candidate became LIVE at once, and the job ended with outcome
  `REGISTERED`.
- **Now:** `DELIVERY_STRATEGY` decides, and the default is `shadow`: the candidate runs beside
  LIVE without serving, and waits for approval (`oran-adapt rollout approve`). A job that
  starts a rollout ends `COMPLETED` with outcome `DELIVERING`.
- **To keep the pilot's behaviour, set `DELIVERY_STRATEGY=blue_green`.** LIVE moves at once,
  and the job ends with outcome `REGISTERED` as before.
- `canary` and `ab` need a deployment adapter with the `traffic_split` feature. Config lint
  and startup refuse any other combination. See
  [integration-guide.md](integration-guide.md#2-promotion-to-serving-deployment-adapters) and
  [adapters/rollout_metrics.md](adapters/rollout_metrics.md).
- `DEPLOYMENT_BACKEND=registry-alias` (the default), with `DEPLOYMENT_ALIAS` unset, moves the
  `LIVE_ALIAS` alias (`live`) exactly as the pilot did.

## 5. The LLM needs two keys

- **Pilot:** `LLM_PROVIDER=anthropic` turned the LLM on.
- **Now:** the LLM stays off unless `LLM_ENABLED=true` is also set. It is fenced by call caps,
  a token budget and a circuit breaker, and every fallback is recorded. `LLM_PROVIDER` also
  accepts `openai-compatible`.
- **A pilot with `LLM_PROVIDER=anthropic` and no `LLM_ENABLED` runs without the LLM after
  migration.** The pipeline does not depend on it, so nothing fails: the heuristic path runs
  and the reports say so. See [adapters/llm.md](adapters/llm.md).

## 6. Authentication and authorization

- `AUTH_ENABLED=true` and `API_KEYS` work as in the pilot. Make new keys with
  `oran-adapt auth new-key --role ...`, which prints the `API_KEYS` entry (a hash, not the
  key).
- **Authorization is deny-by-default.** Each route needs an action (`read`, `submit`, `data`,
  `promote` or `admin`). `POLICY_ROLES` maps actions to roles, and an action missing from it
  is allowed to nobody. The default mapping lets `READ_ONLY` read, lets `ML_ENGINEER` and
  `OPERATOR` submit, lets `ML_ENGINEER` manage data, lets `OPERATOR` promote, and gives `ADMIN`
  everything. Check each client's role against
  [security/authz-matrix.md](security/authz-matrix.md) before switching over.
- New identity backends are `oidc`, `gateway` and `mtls` (`AUTH_BACKEND`). New policy
  backends are `static-rbac` (the default) and `opa` (`POLICY_BACKEND`). Rate limits are on by
  default. See [security.md](security.md).

## 7. Storage and services

- **MinIO is gone** from compose, and so are `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`.
  Artifacts go through the artifact store (`ARTIFACT_STORE_BACKEND`: `filesystem` or
  `fsspec`) and the registry's own storage. Copy any artifacts the pilot kept only in MinIO
  before removing its volume.
- Compose adds the `worker` and `migrate` services.
- **Production refuses defaulted storage locations.** With `ENVIRONMENT=production`,
  `DATABASE_URL`, `ARTIFACT_WORKDIR` and every selected adapter's production keys (for
  example `MLFLOW_TRACKING_URI`) must be set explicitly. The pilot's SQLite defaults are for
  development only.

## 8. Retired keys are ignored silently

`Settings` ignores unknown environment variables, so a pilot `.env` still loads. The retired
keys have **no effect**, and nothing warns about them:

| Pilot key | Replacement |
|---|---|
| `VALIDATION_ACCURACY_TOLERANCE` | `GATE_POLICY` / `GATE_POLICY_FILE` (step 3) |
| `VALIDATION_RMSE_TOLERANCE_RATIO` | `GATE_POLICY` / `GATE_POLICY_FILE` (step 3) |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | none (step 7) |

Check what is really in force with `oran-adapt config effective`, or
`GET /api/v1/config/effective`. It lists every key's value and its source. A configuration
**file** is stricter: `oran-adapt config lint` rejects unknown keys in it.

## Checklist

1. Back up the database and any MinIO data.
2. Start from `.env.example` or a file in `config/examples/`, and carry the pilot values
   across. Drop the keys in step 8.
3. Choose `GATE_POLICY` (step 3) and `DELIVERY_STRATEGY` (step 4). Use `blue_green` to keep
   the pilot's immediate promotion.
4. Set `LLM_ENABLED=true` if the pilot used the LLM.
5. Run `oran-adapt config lint` on the file, and `oran-adapt config effective` in the target
   environment.
6. Run `oran-adapt db upgrade`, then `oran-adapt db status`.
7. Start the API and at least one worker. Check `GET /api/v1/ready`.
8. Point clients at `GET /api/v1/adaptation/jobs/{job_id}` for results (step 2).
9. Post one known drift event, and follow its job to a terminal status.
