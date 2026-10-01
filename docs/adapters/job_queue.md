# Job queue adapters

Adaptation jobs no longer run inside the API. `POST /api/v1/adaptation/events` records the job
`QUEUED` and returns `201`; a **worker** claims it, runs it and records its outcome. A job queue
adapter implements `oran_adapt.ports.JobQueuePort`: it only *wakes* a worker for a queued job.
`JOB_QUEUE_BACKEND` selects it; the core finds adapters through the `oran_adapt.job_queue`
entry-point group (`bootstrap.build_job_queue`).

Shipped adapters:

| Adapter | Module | Wakes a worker by | Needs | Verified |
|---|---|---|---|---|
| `database` (default) | `adapters/job_queues.py` | nothing: workers poll the job table | - | local |
| `inline` | `adapters/job_queues.py` | running the job inside the submitting call | - (development only; production refuses it) | local |
| `celery` | `adapters/job_queues.py` | task `JOB_QUEUE_CELERY_TASK` on queue `<JOB_QUEUE_NAME_PREFIX><class>` | `JOB_QUEUE_CELERY_BROKER_URL`; extra `[celery]` | app double, unverified against a broker |
| `rq` | `adapters/job_queues.py` | `run_job_by_id(job_id)` on Redis Queue `<prefix><class>` | `JOB_QUEUE_RQ_REDIS_URL`; extra `[rq]` | client double, unverified against Redis |
| `kubernetes` | `adapters/job_queues.py` | a `batch/v1` Job per attempt running `oran-adapt worker run-job --job-id <id>` | `JOB_QUEUE_K8S_IMAGE`, the `K8S_*` API settings | mock transport, unverified against a cluster |

## The database is the queue

The `adaptation_job` table holds every job; a broker message is only a hint. That is why a
message that is lost, late or delivered twice can neither lose nor double a job:

* **Claim.** A worker takes a job with one conditional `UPDATE ... WHERE status='QUEUED' AND
  lease_token IS NULL` that only one worker can win, writing a fresh `lease_token`, its
  `lease_owner` and `lease_expires_at = now + JOB_LEASE_TTL_S`. Candidates are tried in order
  of priority (from the event severity, `JOB_PRIORITY_BY_SEVERITY`), then age; only jobs of the
  worker's classes (`JOB_WORKER_CLASSES` / `--classes`) and past their `available_at`.
* **Fence.** Every write a worker makes to its job names its lease token. A worker whose lease
  was taken away (it paused, or the reaper decided it was dead) gets `JobLeaseLostError` and its
  late result is dropped: a job has exactly one outcome.
* **Checkpoints.** Every `JOB_HEARTBEAT_S` the attempt's supervisor renews the lease, the model
  lock and the tenant slot, and stops the attempt on a cancel request, the deadline or a drain.
  In `process` mode (`JOB_EXECUTION_MODE`) stopping kills the attempt's whole process tree.
* **Reaper.** Every worker runs it each `JOB_REAP_INTERVAL_S` (also `oran-adapt jobs reap`):
  * a lease not renewed for `JOB_LEASE_TTL_S` is lost: the job is requeued with backoff, or
    quarantined (below), or - if it was `REGISTERING` or `PROMOTING` - failed with
    `JOB_ABANDONED` and `needs_reconciliation`, since the registry may already hold its version;
  * a queued job past its deadline is recorded `TIMED_OUT`;
  * a queued job not claimed for `JOB_REPUBLISH_AFTER_S` (or never published because the broker
    was down) is published again.

## Outcomes

| What happens | Result |
|---|---|
| attempt finishes | `COMPLETED` / `REJECTED` / `ROLLED_BACK` ..., as before |
| transient error (registry, database unreachable) | requeued after `JOB_RETRY_BACKOFF_S * 2^(n-1)`, up to `JOB_MAX_RETRIES`, then `FAILED` |
| worker process killed, lease lost | requeued (`lost_count` + 1); at `JOB_POISON_THRESHOLD` losses: `FAILED`, `JOB_QUARANTINED`, `quarantined=true` |
| lost while registering or promoting | `FAILED`, `JOB_ABANDONED`, `needs_reconciliation` |
| `JOB_TIMEOUT_S` per attempt, or `JOB_DEADLINE_S` since submission | `TIMED_OUT`; the attempt's process tree is killed |
| `POST /adaptation/jobs/{id}/cancel` on a queued job | `CANCELLED` at once (200) |
| ... on a running job | `cancel_requested`, 202; `CANCELLED` within one checkpoint |
| ... on an ended, registering or promoting job | 409 `JOB_NOT_CANCELLABLE` |
| worker drained (SIGTERM, Ctrl+C, Ctrl+Break) | the running job gets `JOB_DRAIN_TIMEOUT_S`, then is requeued without spending an attempt |

`JOB_TENANT_CONCURRENCY` / `JOB_TENANT_LIMITS` cap how many of a tenant's jobs run at once (the
`job_slot` table; its primary key makes the cap exact across workers). The tenant is the
submitting principal mapped through `JOB_TENANT_BY_PRINCIPAL`, else `JOB_DEFAULT_TENANT`. The
worker class is `JOB_CLASS_BY_FRAMEWORK[<model framework>]`, else `JOB_DEFAULT_CLASS`: run GPU
workers with `oran-adapt worker run --classes gpu` on GPU nodes.

## Running workers

```
oran-adapt worker run [--classes default,gpu] [--once] [--max-jobs N]   # poll and run
oran-adapt worker run-job --job-id <id>                                   # one attempt (brokers)
oran-adapt jobs list [--status QUEUED] [--quarantined]
oran-adapt jobs cancel --job-id <id>
oran-adapt jobs reap
GET  /api/v1/adaptation/jobs?status=&model_id=&tenant=&quarantined=&limit=&offset=
POST /api/v1/adaptation/jobs/{id}/cancel
```

Run any number of workers against one database. With `celery`, the Celery worker registers the
task that calls the same entry point (`templates/job-queue-worker/celery_app.py`):

```python
@app.task(name="oran_adapt.run_job", acks_late=True)
def run_job(job_id):
    from oran_adapt.orchestrator.worker import run_job_by_id
    return run_job_by_id(job_id)
```

and runs `celery -A celery_app worker -Q oran-jobs-default`. With `rq`:
`rq worker oran-jobs-default --url $JOB_QUEUE_RQ_REDIS_URL`. Metrics:
`job_queue_depth{worker_class}`, `job_queue_oldest_age_seconds{worker_class}`, `job_requeues_total{reason}`,
`job_quarantined_total`, `job_cancelled_total{where}`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `JOB_QUEUE_BACKEND` | `database` | the adapter |
| `JOB_EXECUTION_MODE` | `process` | `process` (killable) or `thread` (tests only) |
| `JOB_HEARTBEAT_S` | `5` | checkpoint interval (cancel latency) |
| `JOB_LEASE_TTL_S` | `30` | lease; must exceed 2 x heartbeat |
| `JOB_POLL_INTERVAL_S` / `JOB_REAP_INTERVAL_S` | `1` / `10` | idle poll / reaper |
| `JOB_REPUBLISH_AFTER_S` | `300` | republish an unclaimed job |
| `JOB_MAX_RETRIES` / `JOB_RETRY_BACKOFF_S` | `2` / `1` | transient-error retries |
| `JOB_POISON_THRESHOLD` | `3` | lost attempts before quarantine |
| `JOB_TIMEOUT_S` / `JOB_DEADLINE_S` / `JOB_KILL_GRACE_S` | `600` / unset / `5` | per attempt / since submission / terminate-to-kill |
| `JOB_DRAIN_TIMEOUT_S` | `30` | how long a draining worker lets its job run |
| `JOB_DEFAULT_CLASS` / `JOB_CLASS_BY_FRAMEWORK` / `JOB_WORKER_CLASSES` | `default` / `{}` / `["default"]` | worker classes |
| `JOB_PRIORITY_BY_SEVERITY` / `JOB_DEFAULT_PRIORITY` | CRITICAL 30 ... LOW 0 / `0` | claim order |
| `JOB_CLAIM_CANDIDATES` | `10` | candidates tried per poll |
| `JOB_TENANT_BY_PRINCIPAL` / `JOB_DEFAULT_TENANT` | `{}` / `default` | tenants |
| `JOB_TENANT_CONCURRENCY` / `JOB_TENANT_LIMITS` | `0` (unlimited) / `{}` | running jobs per tenant |
| `JOB_QUEUE_NAME_PREFIX` | `oran-jobs-` | broker queue per class |
| `JOB_QUEUE_CELERY_BROKER_URL` / `JOB_QUEUE_CELERY_TASK` | unset / `oran_adapt.run_job` | Celery |
| `JOB_QUEUE_RQ_REDIS_URL` | unset | RQ |
| `JOB_QUEUE_K8S_IMAGE` / `_ENV_SECRET` / `_SERVICE_ACCOUNT` / `_CLASS_PODS` / `_TTL_AFTER_FINISHED_S` | unset / unset / unset / `{}` / `3600` | Kubernetes Jobs |

## Writing a new adapter

1. Implement `runs_inline` (False), `ping()` and `publish(job: QueuedJob)`. `publish` hands the
   job id to the broker, routed by `job.worker_class`, and raises `JobQueueUnavailableError`
   (never an SDK exception) when the broker cannot be reached. The job is already safely
   `QUEUED`: the reaper publishes it again later.
2. Accept a publish of the same job twice: the claim, not the broker, keeps a job from running
   twice. Whatever consumes the message calls `oran_adapt.orchestrator.worker.run_job_by_id`.
3. Import the SDK inside the factory only, under `oran_adapt/adapters/`, and add it to
   `SDK_HOMES` in `tests/unit/test_import_boundary.py`. A missing SDK is a `ConfigurationError`
   naming the `pip install` extra.
4. Declare an `AdapterSpec(port="job_queue", ...)` with its `config_keys` and `required_keys`;
   register it under `[project.entry-points."oran_adapt.job_queue"]`.
5. Run the conformance suite and list the adapter in the table above.

## Conformance suite

`oran_adapt.conformance.job_queue` checks `protocol`, `publish_delivers` (exactly one message
per publish), `routes_by_worker_class`, `republish_is_safe` and, with `break_broker`,
`unreachable_broker` (`JobQueueUnavailableError` from `publish` and `ping`):

```python
from oran_adapt.conformance.job_queue import Context, run
run(MyQueue(...), Context(delivered=my_broker.messages, break_broker=my_broker.stop))
```

`tests/unit/test_phase6_execution.py::test_job_queue_conformance` runs it for every shipped
adapter.
