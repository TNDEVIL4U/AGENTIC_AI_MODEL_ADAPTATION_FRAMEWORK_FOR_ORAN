# Hardening Phase 7 report: validation gate and progressive delivery

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. No serving system or
Prometheus server was used:
- the gate ran on real fitted models;
- the rollout controller ran for real against the filesystem registry with the
  `registry-alias` traffic split, on SQLite;
- the `webhook` split ran against the stdlib serving stub, and the `kserve` split against the
  Kubernetes API emulator;
- the `prometheus` rollout metrics adapter ran against a Prometheus HTTP API double through
  `httpx.MockTransport`.

**Traffic splitting on a real KServe or webhook serving system, and Prometheus queries against a
real Prometheus, are unverified.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 6: the gate passes a candidate up to a tolerance **worse** than the incumbent (`VALIDATION_ACCURACY_TOLERANCE`, `VALIDATION_RMSE_TOLERANCE_RATIO`) | Both keys are removed. `validation/gate.py` compares candidate and incumbent with a **paired bootstrap** over the same held-out rows. Under the default `superiority` policy the 95 % interval of the improvement must lie above the margin (0), so an equal or marginally worse candidate is rejected. `non_inferiority` is available as an explicit policy choice |
| The primary metric is "the first key of the metrics dict" | The metric comes from the task type (accuracy for classifiers, RMSE for regressors, and so on). Its direction is known, and the interval is always oriented so positive means better |
| No guardrails | Guardrails reject on their own, whatever the primary metric says: per-slice regression (`slices`), calibration (ECE), prediction latency, serialized size and `metric_guards` |
| Decisions are not recorded | Each decision is a `gate_decision` row with the full evidence, an audit entry `GATE_DECIDED`, a metric `gate_decisions_total{verdict,mode}` and `GET /api/v1/models/{id}/gate-decisions`. Each carries the policy's `version` and `policy_hash` |
| Thresholds are loose settings | `GATE_POLICY` and `DELIVERY_POLICY` are versioned, strictly validated policies (unknown keys refused). They are inline JSON or TOML/JSON files (`config/policies/gate.toml`, `delivery.toml`). A rollout stores the policy it started under |
| One step switches 100 % of traffic: no shadow, canary, blue/green or approval | `DELIVERY_STRATEGY`: `shadow` (default), `canary`, `ab`, `manual` and `blue_green`. The last is the previous immediate promotion, now with read-back |
| No automatic rollback on online evidence | The rollout controller (`delivery/controller.py`) judges each arm's online metrics against `DELIVERY_POLICY.health` on every tick. A breach removes the split, **reads the serving system back** and ends `ROLLED_BACK`. LIVE never moved, so nothing else needs undoing |
| No approval step | `manual` rollouts wait in `AWAITING_APPROVAL` for `POST /rollouts/{id}/approve` or `reject` (and `oran-adapt rollout approve|reject`) within `approval_ttl_s`. After that they are `EXPIRED`, and a late approval is refused with 409 `ROLLOUT_STATE_CONFLICT` |

Migration `0009_gate_and_rollouts` creates `gate_decision`, `rollout` and `rollout_observation`.
Its downgrade is tested (`test_migration_0009_up_and_down`).

How the pipeline changes:
- A job whose candidate passes the gate registers it. With `blue_green` it promotes at once and
  ends `REGISTERED` as before. Otherwise it starts a rollout and ends `COMPLETED` with outcome
  `DELIVERING`.
- While a model has an active rollout, a new event for it ends with outcome
  `ROLLOUT_IN_PROGRESS`. It starts no analysis and no training.

## 2. Ports and adapters

**Traffic split: a new optional part of `DeploymentPort`.** A deployment adapter with the
`traffic_split` feature implements `TrafficSplitPort`:
- `split(model, stable, candidate, percent)`;
- `traffic(model)`.

`Deployer.split` applies a split and reads it back, just as `Deployer.rollout` does for a single
version. A split that does not read back raises `DeploymentError`. At startup, a strategy that
needs a split (`canary`, `ab`, and `shadow`/`manual` continuing as a canary) is refused with
a `ConfigurationError` when the backend has none. The error names the backends that have one.

| Deployment adapter | How a split is expressed | Test double |
|---|---|---|
| `registry-alias` | the candidate version on `DEPLOYMENT_CANARY_ALIAS`, the percentage in the version tag `DEPLOYMENT_TRAFFIC_TAG` (serving reads both) | real (filesystem registry) |
| `webhook` | `POST /traffic` with `{stable, candidate, percent}`; `GET /traffic` reads back | stdlib serving stub |
| `kserve` | `canaryTrafficPercent` on the InferenceService, with the candidate as the latest revision | Kubernetes API emulator |

**New port: `RolloutMetricsPort`** (`ping`, `observe(session, window) -> ArmStats`). It reports
what each arm of a rollout showed online over a time window.

| Adapter | Metrics come from | Test double |
|---|---|---|
| `api` (default) | `POST /api/v1/rollouts/{id}/observations`, stored as `rollout_observation` rows | real (SQLite) |
| `prometheus` | a PromQL range query per metric, with `{model}`, `{version}`, `{arm}`, `{rollout_id}` and `{window_s}` filled in | `httpx.MockTransport` Prometheus double |

- Guide: `docs/adapters/rollout_metrics.md`, which covers the gate, the strategies, health, the
  API and CLI, and every key. The traffic-split section is in `docs/adapters/deployment.md`.
- Template: `templates/rollout-metrics-adapter/`, a JSON-lines source.
- Conformance suite: `oran_adapt.conformance.rollout_metrics`, with seven checks. `api`,
  `prometheus` and the template all pass it.

The controller:
- A tick runs from the worker loop every `ROLLOUT_TICK_S`, or from `oran-adapt rollout tick`.
- Each tick holds the model's lock under the holder `rollout:<id>`.
- Promotion goes through `promote_version` with the idempotency key `rollout:<id>:promote` and
  `expected_live = stable`.
- If the metrics source is unreachable or a split removal fails, the rollout stays where it is,
  with a note in its history.

New metrics:
- `traffic_splits_total{backend,outcome}`;
- `gate_decisions_total{verdict,mode}`;
- `rollouts_started_total{strategy}`;
- `rollouts_finished_total{strategy,state}`;
- `rollouts_active{state}`;
- `rollback_total{trigger="rollout"}`.

New errors:
- `ROLLOUT_NOT_FOUND` (404);
- `ROLLOUT_STATE_CONFLICT` (409);
- `ROLLOUT_METRICS_UNAVAILABLE` (503).

New audit actions:
- `GATE_DECIDED`;
- `ROLLOUT_STARTED`;
- `ROLLOUT_ADVANCED`;
- `ROLLOUT_FINISHED`.

New notification events: `rollout.<state>`.

New API routes:
- `GET /api/v1/rollouts`;
- `GET /api/v1/rollouts/{id}`;
- `POST /api/v1/rollouts/{id}/approve|reject` (permission `promote`);
- `POST /api/v1/rollouts/{id}/observations` (permission `submit`);
- `GET /api/v1/models/{id}/gate-decisions`.

## 3. Configuration keys

Added:
- `GATE_POLICY`, `GATE_POLICY_FILE`;
- `DELIVERY_STRATEGY`, `DELIVERY_POLICY`, `DELIVERY_POLICY_FILE`;
- `ROLLOUT_METRICS_BACKEND`, `ROLLOUT_TICK_S`;
- `ROLLOUT_PROMETHEUS_URL`, `ROLLOUT_PROMETHEUS_TOKEN` (secret), `ROLLOUT_PROMETHEUS_QUERIES`,
  `ROLLOUT_PROMETHEUS_REQUESTS_QUERY`, `ROLLOUT_PROMETHEUS_STEP_S`, `ROLLOUT_PROMETHEUS_TIMEOUT_S`;
- `DEPLOYMENT_CANARY_ALIAS`, `DEPLOYMENT_TRAFFIC_TAG`.

Removed:
- `VALIDATION_ACCURACY_TOLERANCE`;
- `VALIDATION_RMSE_TOLERANCE_RATIO`.

An installation that relied on the tolerance sets `GATE_POLICY={"mode": "non_inferiority",
"margin": 0.02}`.

Every threshold of the gate and of delivery is a field of `GatePolicy` or `DeliveryPolicy`
(`core/policies.py`), documented in `docs/adapters/rollout_metrics.md`.

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Marginally-worse candidate rejected, decision recorded | PASS | `phase7.py` check 1: a candidate with one percentage point more errors gives delta −0.0100, 95 % CI [−0.0225, −0.0025] vs margin 0, so REJECT. The row, the `GATE_DECIDED` audit and the API listing carry the policy hash. Unit: `test_marginally_worse_candidate_is_rejected_with_its_reasons`, `test_equal_candidate_is_rejected_by_superiority_and_accepted_by_non_inferiority`, `test_gate_decisions_are_recorded_and_listed`, `test_the_gate_is_reproducible` |
| 2 | Clear improvement accepted | PASS | `phase7.py` check 2: delta +0.3125, CI low +0.27, so ACCEPT; the calibration, latency and size guardrails pass. Unit: `test_clear_improvement_is_accepted` |
| 3 | Guardrail breach rejects | PASS | `phase7.py` check 3: a candidate better overall whose "far" slice gets 28.5 points worse is rejected by `slice:cell`. Unit: `test_a_slice_regression_rejects_a_better_candidate` (also a required slice column that is missing), `test_a_size_guardrail_breach_rejects_a_better_candidate` |
| 4 | Canary success path | PASS | `phase7.py` check 4: 10 % → 50 % → 100 % on healthy metrics, PROMOTED, LIVE = candidate, split cleared and read back. Unit: `test_canary_success_walks_the_steps_and_promotes` (hold time, a fresh window per step, audit trail, `rollout.promoted` event), `test_deliver_starts_a_canary_and_reports_delivering`, `test_single_step_canary_promotes_at_once` |
| 5 | Canary breach → automatic rollback, served version verified | PASS | `phase7.py` check 5: candidate error_rate 0.20 vs stable 0.01 gives ROLLED_BACK on the next tick. The serving system reads back stable only, and LIVE is unchanged. Unit: `test_canary_breach_rolls_back_and_the_served_version_reads_back`, `test_webhook_split_reads_back_and_a_failed_split_rolls_back`, `test_a_promotion_racing_a_moved_live_rolls_back` |
| 6 | Approval path, including expiry | PASS | `phase7.py` check 6: approval within the 600 s TTL promotes. An approval at 601 s is refused and the rollout becomes EXPIRED, with LIVE kept. Unit: `test_manual_approval_promotes`, `test_approval_after_expiry_is_refused_and_expires`, `test_unanswered_approval_expires_on_tick`, `test_approval_then_canary_and_reject`, `test_rollout_api`, `test_rollout_cli` |
| 7 | Unknown-Stack Protocol | PASS | `phase7.py` check 7: the import boundary, the guide naming every rollout metrics adapter, and the conformance suite (16 cases: `api`, `prometheus`, template). Unit: `test_conformance_catches_a_source_that_mixes_arms`, `test_startup_refuses_a_strategy_the_backend_cannot_split`, `test_kserve_split_uses_canary_traffic_percent` |
| – | A/B, shadow, busy and unavailable ticks, worker loop, one rollout per model | PASS | `test_ab_decides_by_significance` (better and worse), `test_shadow_healthy_hands_over_to_approval`, `test_shadow_without_a_verdict_expires`, `test_tick_is_busy_while_the_model_is_locked`, `test_unreachable_metrics_leave_the_rollout_unchanged`, `test_the_worker_ticks_rollouts`, `test_a_model_with_a_rollout_takes_no_new_adaptation`, `test_health_rules`, `test_ab_test_verdicts`, `test_policy_files_load_and_bad_ones_are_refused` |

The unit tests are all in `tests/unit/test_phase7_gate_delivery.py` (38 test functions, 52 cases).
The test fixtures in `tests/conftest.py` and `test_phase8_validation.py` pin
`delivery_strategy="blue_green"` and a non-inferiority gate. The older pipeline tests therefore
keep testing the pipeline rather than delivery.

## 5. Hardcoding

- **C13 closed.** The tolerance keys are gone, replaced by the versioned gate policy.
- **C14 partly closed.** The validation thresholds now live in policy files. The `ANALYSIS_*`,
  `REUSE_*`, `DECISION_*`, `TORCH_*` and `JOB_*` keys stay schema defaults (see
  `docs/hardcoding-inventory.md`, "Hardening Phase 7 status").
- **Kept on purpose:**
  - the rollout id format (`uuid4().hex`);
  - the holder `rollout:<id>` and the idempotency key `rollout:<id>:promote`;
  - the 50-metric cap per observation;
  - the list bound of 1000.
- Inventory burn-down: C open 8 → 7.

## 6. Assumptions and defaults

These are recorded in `docs/OPEN-QUESTIONS.md` ("Validation gate and progressive delivery"):
- **Superiority with margin 0 at 95 %.** A retrained model must be demonstrably better to
  replace the incumbent.
- **`shadow` is the default strategy.** It needs no traffic split, so it works with every
  deployment backend. A healthy shadow then waits for approval (`shadow_then = "manual"`).
- **Health defaults:**
  - `error_rate` may be at most 0.01 worse than stable;
  - `latency_p95_ms` at most 1.5× stable (optional);
  - at least 100 requests per arm before any verdict.
- **Canary steps 5/25/50/100 %, each held 10 minutes.** A/B runs at 50 % for 1 h with a Welch
  t-test at 95 %, and an inconclusive result rolls back. Approval waits 24 h.
- **A canary step that never collects `min_samples` waits indefinitely.** Only shadow and approval
  have deadlines; an operator can reject.
- **A tick holds the model's lock briefly.** A drift event submitted for the same model at that
  moment gets 409 `MODEL_BUSY`, as when a job holds the lock; nothing is recorded, and the caller
  resends the event.
- **A `shadow` rollout counts as needing a split when `shadow_then = "canary"`, and a `manual`
  one when `approval_then = "canary"`,** so startup refuses them on a backend without splitting
  even though the rollout might end before the canary.
- **A first version, with no incumbent, is judged on its own** and accepted when it can be scored.

## 7. Unverified locally

- **Traffic splitting on a real KServe cluster and on a real webhook serving system.** Only the
  Kubernetes emulator and the stdlib stub were used. The `registry-alias` split needs the serving
  layer to honour the canary alias and tag. Nothing in this repository serves traffic, so the
  percentages are recorded, not enforced, until such a layer exists.
- **Prometheus against a real server.** The double answers the HTTP API as the adapter calls it.
  The example PromQL in the guide has not been run.
- **Other deployment adapters** (`seldon`, `k8s`, `sagemaker`, `vertex`, `bentoml`, `triton`,
  `gitops`) do not declare `traffic_split`. Startup refuses canary and A/B with them.
- **Rollout ticks on PostgreSQL with several workers.** The lock is the same `model_lock` row
  used by jobs, but only SQLite was used.

## 8. Gate

`scripts/verify.sh 7`: **PASS in 168 s**, run locally on 2026-09-29 (budget 300 s). It covers:
- ruff;
- mypy, with 0 errors in 154 source files;
- the import boundary (2 passed);
- the no-gaps lint;
- the scoped tests (validation, registry, orchestrator: 291 passed in 78.1 s);
- the smoke tier (257 passed in 46.5 s), with 2 workers;
- acceptance, 7/7 (gate and record 8 s, others 0–5 s).

The worker-tick test was added after that run, and passed on its own.
