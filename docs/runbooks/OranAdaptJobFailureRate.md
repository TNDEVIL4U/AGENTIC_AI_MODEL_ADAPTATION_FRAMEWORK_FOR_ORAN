# OranAdaptJobFailureRate

**Fires when** more than 25 % of the adaptation jobs that finished in the last hour ended
`FAILED` (`adaptation_jobs_total{status="FAILED"}`), with at least four finished, for 15 minutes.

**Impact.** Drifted models are not being adapted; they stay in service at their current
version. A failed job changed nothing live (one that failed in `REGISTERING` or `PROMOTING` is
marked `needs_reconciliation`).

**Check.**
1. `adaptation_failure_total` by `reason` names the error codes. One dominant code is one
   cause: `MLFLOW_UNAVAILABLE` (the registry), `DATA_SOURCE_UNAVAILABLE` (the dataset store),
   `JOB_TIMEOUT` (see OranAdaptStageSlow), `VALIDATION_FAILED` (candidates the gate rejected).
2. `oran-adapt jobs list --status FAILED` and `GET /api/v1/adaptation/jobs/{job_id}`: the recorded error,
   its context and the stage it failed in.
3. The job's trace: its trace id is derived from the event (`docs/operations/observability.md`),
   and the failing span carries the exception.
4. `adapter_errors_total` by `port`, `adapter` and `code`: an external system failing.

**Fix.** Fix the dependency or the data the codes point at, then submit a new drift event for
each affected model (a resubmitted event with the same `event_id` returns the failed job).
