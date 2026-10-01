# OranAdaptJobQuarantined

**Fires when** a job was quarantined in the last hour (`job_quarantined_total`): it ran
`JOB_POISON_THRESHOLD` times without reaching an outcome, usually because its worker died each
time (out of memory, node loss, a crash in native code).

**Impact.** That job no longer runs; other jobs are unaffected.

**Check.**
1. `oran-adapt jobs list --quarantined` gives the job id; `GET /api/v1/adaptation/jobs/{job_id}` its transitions.
2. The worker pods' restarts and `OOMKilled` reasons around the attempt times.
3. The data version and model the job used: an oversized dataset is the common cause
   (`DATASET_MAX_ROWS`, `DATASET_MAX_SOURCE_BYTES`).

**Fix.** Give the pool more memory or cap the data, then resubmit the drift event with a new
`event_id`. The quarantined job stays as the record.
