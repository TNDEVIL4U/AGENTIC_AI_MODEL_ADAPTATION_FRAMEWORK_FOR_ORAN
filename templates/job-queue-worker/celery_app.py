"""A Celery worker for JOB_QUEUE_BACKEND=celery (docs/adapters/job_queue.md).

The API publishes task JOB_QUEUE_CELERY_TASK with the job id on queue
<JOB_QUEUE_NAME_PREFIX><worker class>. This app registers that task; the task runs one attempt
of the job through the same worker code as `oran-adapt worker run`, so the lease, fencing,
deadline and cancel checkpoints apply. Run it with the framework's configuration in the
environment (the same variables as the API):

    pip install 'oran-adapt[celery]'
    celery -A celery_app worker -Q oran-jobs-default --concurrency 1

A duplicated or late message is harmless: the job is claimed in the database, and a message for
a job that is no longer claimable does nothing.
"""

from __future__ import annotations

from celery import Celery

from oran_adapt.core.config import get_settings

settings = get_settings()
broker = settings.job_queue_celery_broker_url
app = Celery("oran_adapt_worker", broker=broker.get_secret_value() if broker else None)
# One message is acknowledged only after the task ran: a worker killed mid-task leaves the
# message to be redelivered (the reaper would also requeue the job once its lease runs out).
app.conf.task_acks_late = True
app.conf.worker_prefetch_multiplier = 1


@app.task(name=settings.job_queue_celery_task)
def run_job(job_id: str) -> bool:
    from oran_adapt.orchestrator.worker import run_job_by_id

    return run_job_by_id(job_id)
