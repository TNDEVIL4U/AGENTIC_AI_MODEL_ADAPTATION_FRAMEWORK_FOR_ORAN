# Job queue worker templates

Workers run queued adaptation jobs. Pick the one matching `JOB_QUEUE_BACKEND`
(docs/adapters/job_queue.md):

| Backend | Worker |
|---|---|
| `database` | `oran-adapt worker run [--classes default]` - no broker, polls the job table |
| `celery` | `celery -A celery_app worker -Q oran-jobs-default` with `celery_app.py` from this folder |
| `rq` | `rq worker oran-jobs-default --url "$JOB_QUEUE_RQ_REDIS_URL"` (the message names `oran_adapt.orchestrator.worker.run_job_by_id`, nothing to register) |
| `kubernetes` | none: each attempt is a Job running `oran-adapt worker run-job --job-id <id>` in `JOB_QUEUE_K8S_IMAGE` |

Every worker needs the framework's configuration (database URL, registry, LLM keys) in its
environment, exactly like the API. Run GPU workers on GPU nodes with the `gpu` class
(`--classes gpu`, queue `oran-jobs-gpu`) and map frameworks to it with `JOB_CLASS_BY_FRAMEWORK`.

Stop workers with SIGTERM (Ctrl+C / Ctrl+Break on Windows): the running job gets
`JOB_DRAIN_TIMEOUT_S` to finish and is otherwise put back in the queue. With a broker, a
database-polling worker is still useful as a fallback: the reaper republishes, but any worker
can claim.

The Celery template is unverified against a running broker in this repository's CI.
