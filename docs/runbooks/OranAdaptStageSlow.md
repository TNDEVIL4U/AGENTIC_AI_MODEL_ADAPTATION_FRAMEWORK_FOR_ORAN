# OranAdaptStageSlow

**Fires when** the 95th percentile of a pipeline stage's duration
(`adaptation_stage_duration_seconds`, by `stage`) is above 30 minutes over the last hour, for
30 minutes.

**Impact.** Jobs take longer and those near `JOB_TIMEOUT_S` time out
(`job_timeouts_total{stage=...}`); each running job holds its model's lock and a tenant slot.

**Check.**
1. Which stage: `DATA_PREPARING` (dataset reads, `dataset_read_duration_seconds`), `ADAPTING`
   (training), `VALIDATING_CANDIDATE` (gate scoring), `PROMOTING` (the serving read-back, up to
   `DEPLOYMENT_TIMEOUT_S`).
2. The traces of slow jobs: the stage span's children show which adapter call took the time
   (`adapter_call_duration_seconds` has the same split).
3. Dataset growth: `dataset_rows_read_total` and `dataset_bytes_read_total` rising with the
   duration means the training data grew.

**Fix.** Bound the data read (`DATASET_MAX_ROWS`), give the worker class more CPU
(`workers[].resources`), move heavy frameworks to their own class (`JOB_CLASS_BY_FRAMEWORK`),
or raise `JOB_TIMEOUT_S` if the duration is expected.
