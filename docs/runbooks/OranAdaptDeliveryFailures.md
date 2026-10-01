# OranAdaptDeliveryFailures

**Fires when** progressive delivery steps of one strategy failed for one reason at least three
times in 30 minutes (`delivery_failures_total`, by `strategy` and `reason`).

**Impact.** By reason:
- `first_split`: the first traffic change did not read back; the rollout was rolled back
  before any traffic moved.
- `serving`: a later split or its undo failed; the tick changed nothing and is retried.
- `metrics`: the rollout metrics source was unavailable; the rollout holds its step.
- `promotion`: LIVE could not move to the candidate; the rollout was rolled back.

**Check.**
1. `GET /api/v1/rollouts?active=true` and each rollout's history: its notes carry the errors.
2. `serving` and `first_split`: `traffic_splits_total{outcome="failed"}` and the serving
   system's state (`DEPLOYMENT_BACKEND`); the `deployment.verify` spans in the rollout's trace
   show the read-back that did not settle.
3. `metrics`: the rollout metrics backend (`ROLLOUT_METRICS_BACKEND`), for example Prometheus
   reachability and its query templates.
4. `promotion`: LIVE moved during the rollout (a manual promotion), or the registry refused the
   alias change.

**Fix.** Restore the serving system or the metrics source; held rollouts resume on the next
tick. A rolled-back rollout needs a new drift event.
