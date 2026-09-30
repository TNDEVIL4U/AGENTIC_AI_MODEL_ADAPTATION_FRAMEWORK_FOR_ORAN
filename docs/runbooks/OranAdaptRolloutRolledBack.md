# OranAdaptRolloutRolledBack

**Fires when** a progressive rollout (canary, A/B) ended `ROLLED_BACK` in the last hour
(`rollouts_finished_total`).

**Impact.** None to traffic: the previous version serves again. The adaptation did not ship.

**Check.**
1. The rollout's record (`GET /api/v1/rollouts/{id}`) holds the step that failed and the
   metric comparison that failed it.
2. `gate_decisions_total` and the job's validation report: a candidate that passed offline
   validation but lost online points to a data shift between the two.
3. `model_deployments_total{outcome="failed"}`: a rollback caused by the serving system itself.

**Fix.** Nothing is required to restore service. Investigate before resubmitting; repeated
rollbacks of one model mean its validation data no longer matches live traffic.
