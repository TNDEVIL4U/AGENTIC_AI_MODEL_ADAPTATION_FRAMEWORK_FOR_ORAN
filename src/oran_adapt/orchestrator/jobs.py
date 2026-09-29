"""Durable adaptation jobs: submission, state transitions and a single pipeline attempt.

The pure pipeline (`orchestrator.pipeline.run_adaptation_job`) only reports its stages
(orchestrator.context). This module adds persistence, idempotency and the fenced transitions a
queued job needs; orchestrator.worker claims queued jobs and runs them.

Submission (`submit_adaptation_job`): the job is inserted RECEIVED together with its model's
lock and moved to QUEUED in the same transaction, so a job is only ever stored queued and
locked. After the commit the job queue adapter (JOB_QUEUE_BACKEND, oran_adapt.job_queue) is
asked to wake a worker; a broker that cannot be reached is logged and the reaper publishes the
job again. The call returns the QUEUED job; the API answers 201 and the caller follows the job
with GET /adaptation/jobs/{id}. Only the development adapter ``inline`` runs the job in the
submitting call and returns its outcome.

Idempotency: every job is keyed by `DriftEvent.idempotency_key()`, enforced by a unique DB
constraint on `AdaptationJob.idempotency_key`. Two callers racing to submit the same event - two
API replicas included - either see each other's row on the initial lookup, or lose the INSERT
race and catch the IntegrityError; either way there is one job and the loser gets it back with
`duplicate=True`.

Locking: a job holds its model's lock (orchestrator.locks) from submission to its end, so only
one job per model is queued or running at a time; a different event for a busy model is refused
with ModelBusyError and nothing is recorded. The worker renews the lock while it runs the job.
An expired lock is taken over only when its holder is neither queued nor running under a live
lease; the holder is then recorded FAILED with JOB_ABANDONED.

Fencing: a worker runs a job under a lease token (orchestrator.worker). Every transition it
records passes the token as ``fence``; once the lease has passed to another worker the token no
longer matches and the transition is refused with JobLeaseLostError, so a job is never taken to
an outcome twice. A fenced stage report also refuses to continue a job whose cancel was
requested (JobCancelledError), unless the job is registering or promoting.

States: every transition goes through core.state_machine. A job goes RECEIVED -> QUEUED, then
per attempt QUEUED -> VALIDATING -> DATA_PREPARING -> ... ; the pipeline reports the stages
after that through orchestrator.context. A retried job goes back to QUEUED.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from oran_adapt.bootstrap import build_job_queue
from oran_adapt.core import metrics
from oran_adapt.core.audit import record_audit
from oran_adapt.core.config import Settings
from oran_adapt.core.correlation import get_correlation_id, set_correlation_id
from oran_adapt.core.enums import TERMINAL_STATUSES, AuditAction, JobStatus
from oran_adapt.core.errors import (
    JobAbandonedError,
    JobCancelledError,
    JobLeaseLostError,
    JobNotCancellableError,
    JobNotFoundError,
    JobQueueUnavailableError,
    JobTimeoutError,
    ModelBusyError,
)
from oran_adapt.core.logging import configure_logging, log_event
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.core.state_machine import check_transition
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    AdaptationEvent,
    AdaptationJob,
    JobSlot,
    ModelLock,
    ModelMetadata,
)
from oran_adapt.llm.client import LlmClient
from oran_adapt.notifications.events import record_job_transition
from oran_adapt.orchestrator.context import JobContext, current_job
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.ports import JobCall, JobExecutorPort, JobQueuePort, ModelRegistryPort, QueuedJob

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Stages where a stopped worker may have changed the registry without finishing: a cancel is
# refused there, and a job lost there is failed for reconciliation instead of being rerun.
_REGISTRY_STAGES = frozenset({JobStatus.REGISTERING, JobStatus.PROMOTING})


def _now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime (SQLite returns them naive)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_response(job: AdaptationJob, *, duplicate: bool) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        model_id=job.model_id,
        status=JobStatus(job.status),
        strategy=job.strategy,
        result=job.result,
        error=job.error,
        duplicate=duplicate,
        created_at=job.created_at,
        updated_at=job.updated_at,
        tenant=job.tenant,
        worker_class=job.worker_class,
        attempt=job.attempt or 0,
        cancel_requested=job.cancel_requested_at is not None,
        quarantined=bool(job.quarantined),
    )


def load_job(session: Session, job_id: str, *, lock: bool = False) -> AdaptationJob:
    """The job row (locked for update when ``lock``); JobNotFoundError when there is none."""
    query = select(AdaptationJob).where(AdaptationJob.job_id == job_id)
    if lock:
        query = query.with_for_update()
    job = session.execute(query).scalar_one_or_none()
    if job is None:
        raise JobNotFoundError(f"job '{job_id}' not found", job_id=job_id)
    return job


def apply_transition(
    session: Session,
    settings: Settings,
    job: AdaptationJob,
    to_status: JobStatus,
    *,
    message: str = "",
    error: dict | None = None,
    result: dict | None = None,
    strategy: str | None = None,
    component: str = "orchestrator",
) -> None:
    """Record ``job`` moving to ``to_status`` in ``session``: the row's status, an immutable
    AdaptationEvent and the transition's notification event (outbox), all committed or rolled
    back together with the caller's transaction."""
    check_transition(job.status, to_status)
    session.add(
        AdaptationEvent(
            job_id=job.job_id,
            component=component,
            from_status=job.status,
            to_status=to_status,
            message=message,
        )
    )
    record_job_transition(
        session, settings, job_id=job.job_id, model_id=job.model_id, from_status=job.status,
        to_status=to_status, message=message, error=error,
        strategy=strategy if strategy is not None else job.strategy,
    )
    job.status = to_status
    if error is not None:
        job.error = error
    if result is not None:
        job.result = result
    if strategy is not None:
        job.strategy = strategy


def check_fence(job: AdaptationJob, fence: str | None, to_status: JobStatus) -> None:
    """Refuse a worker's write once it no longer holds the job's lease, and a stage report
    after a cancel request (outside the registry stages)."""
    if fence is None:
        return
    if job.lease_token != fence:
        raise JobLeaseLostError(
            "this worker no longer holds the job's lease",
            job_id=job.job_id,
            lease_owner=job.lease_owner,
        )
    if (
        job.cancel_requested_at is not None
        and to_status not in TERMINAL_STATUSES
        and to_status != JobStatus.QUEUED
        and JobStatus(job.status) not in _REGISTRY_STAGES
    ):
        raise JobCancelledError(
            "the job was cancelled on request",
            job_id=job.job_id,
            requested_by=job.cancel_requested_by,
            stage=job.status,
        )


def _transition(
    session_factory,
    job_id: str,
    *,
    settings: Settings,
    to_status: JobStatus,
    message: str = "",
    error: dict | None = None,
    result: dict | None = None,
    strategy: str | None = None,
    fence: str | None = None,
) -> JobResponse:
    """Load the job in its own short transaction, record the state change (apply_transition)
    and commit - so a job's persisted status always reflects the last step that actually
    finished, even if the process dies right after. ``fence`` is the caller's lease token
    (check_fence)."""
    with session_scope(session_factory) as session:
        job = load_job(session, job_id, lock=fence is not None)
        check_fence(job, fence, to_status)
        apply_transition(session, settings, job, to_status, message=message, error=error,
                         result=result, strategy=strategy)
        session.flush()
        response = _to_response(job, duplicate=False)
    log_event(logger, message or f"job -> {to_status}", adaptation_job_id=job_id, status=to_status)
    return response


def _fail_abandoned_job(
    session, settings: Settings, old_job_id: str, taken_over_by: str
) -> None:
    """The lock of ``old_job_id`` expired and ``taken_over_by`` now holds it, so the process that
    ran the old job died (a crash or a service restart). Record the old job FAILED in the
    takeover's transaction so it never stays in a running state; its lease is cleared, so a
    worker of it that is still alive has every further write refused."""
    old = session.execute(
        select(AdaptationJob).where(AdaptationJob.job_id == old_job_id)
    ).scalar_one_or_none()
    if old is None or JobStatus(old.status) in TERMINAL_STATUSES:
        return
    last_stage = JobStatus(old.status)
    error = JobAbandonedError(
        "the job's process stopped before the job finished; its model lock expired and was "
        "taken over by a newer job",
        last_stage=last_stage.value,
        taken_over_by=taken_over_by,
    ).to_dict()
    if last_stage in _REGISTRY_STAGES:
        error["context"]["needs_reconciliation"] = True
    apply_transition(session, settings, old, JobStatus.FAILED, message=error["message"],
                     error=error)
    old.lease_token = None
    old.lease_expires_at = None
    session.execute(delete(JobSlot).where(JobSlot.job_id == old_job_id))
    metrics.ADAPTATION_JOBS.labels(JobStatus.FAILED.value, "NONE").inc()
    metrics.ADAPTATION_FAILURE.labels(JobAbandonedError.code).inc()
    log_event(
        logger,
        f"job abandoned while {last_stage.value}; marked FAILED",
        level=logging.WARNING,
        adaptation_job_id=old_job_id,
    )


def _holder_is_active(session: Session, holder_job_id: str | None) -> bool:
    """Whether the job holding an expired lock is still legitimately waiting or running: it is
    queued, or a worker renews its lease. Such a lock is never taken over."""
    if not holder_job_id:
        return False
    holder = session.get(AdaptationJob, holder_job_id)
    if holder is None or JobStatus(holder.status) in TERMINAL_STATUSES:
        return False
    if JobStatus(holder.status) == JobStatus.QUEUED:
        return True
    expires = aware(holder.lease_expires_at)
    return holder.lease_token is not None and expires is not None and expires > _now()


def _placement(session: Session, event: DriftEvent, settings: Settings,
               actor: str) -> dict[str, Any]:
    """Tenant, worker class, priority and deadline of a new job, from configuration."""
    framework = session.scalar(
        select(ModelMetadata.framework).where(ModelMetadata.model_id == event.model_id)
    )
    now = _now()
    return {
        "tenant": settings.job_tenant_by_principal.get(actor, settings.job_default_tenant),
        "worker_class": settings.job_class_by_framework.get(
            framework or "", settings.job_default_class
        ),
        "priority": settings.job_priority_by_severity.get(
            event.severity or "", settings.job_default_priority
        ),
        "deadline_at": (
            now + timedelta(seconds=settings.job_deadline_s)
            if settings.job_deadline_s is not None else None
        ),
        "available_at": now,
    }


def queued_job(job: AdaptationJob) -> QueuedJob:
    return QueuedJob(job_id=job.job_id, worker_class=job.worker_class, tenant=job.tenant,
                     priority=job.priority, attempt=job.attempt)


def publish(session_factory, queue: JobQueuePort, job_id: str) -> bool:
    """Ask ``queue`` to wake a worker for a QUEUED job and record when it did. False when the
    broker could not be reached: the job stays queued and the reaper publishes it again."""
    with session_scope(session_factory) as session:
        job = load_job(session, job_id)
        if JobStatus(job.status) != JobStatus.QUEUED:
            return False
        message = queued_job(job)
    try:
        queue.publish(message)
    except JobQueueUnavailableError as exc:
        log_event(logger, f"could not publish queued job: {exc.message}",
                  level=logging.WARNING, adaptation_job_id=job_id)
        return False
    with session_scope(session_factory) as session:
        job = load_job(session, job_id)
        job.published_at = _now()
    return True


def submit_adaptation_job(
    session_factory,
    event: DriftEvent,
    settings: Settings,
    *,
    registry: ModelRegistryPort | None,
    llm_client: LlmClient | None,
    workdir: str,
    actor: str = "system",
    queue: JobQueuePort | None = None,
) -> JobResponse:
    """Record a drift event as a QUEUED job and wake a worker for it (module docstring). Safe
    to call twice, or concurrently from several API replicas, with events that carry the same
    `idempotency_key()`: every call after the first returns the first call's job with
    `duplicate=True`. Raises ModelBusyError when another job holds the model; an
    infrastructure failure while recording the job (the database unreachable) escapes as an
    exception, since there is nothing to record a job status into in that case.

    ``queue`` is the JobQueuePort adapter (built from JOB_QUEUE_BACKEND when not given). With
    the ``inline`` adapter the job runs here, with ``registry``, ``llm_client`` and
    ``workdir``, and its outcome is returned."""
    key = event.idempotency_key()
    job_id = uuid.uuid4().hex
    queue = queue if queue is not None else build_job_queue(settings)

    with session_scope(session_factory) as session:
        existing = session.execute(
            select(AdaptationJob).where(AdaptationJob.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return _to_response(existing, duplicate=True)

        job = AdaptationJob(
            job_id=job_id,
            idempotency_key=key,
            model_id=event.model_id,
            status=JobStatus.RECEIVED,
            event=event.model_dump(mode="json"),
            correlation_id=get_correlation_id(),
            **_placement(session, event, settings, actor),
        )
        session.add(job)
        try:
            session.flush()
            previous_holder = session.scalar(
                select(ModelLock.job_id).where(ModelLock.model_id == event.model_id)
            )
            if _holder_is_active(session, previous_holder):
                session.rollback()
                raise ModelBusyError(
                    f"model '{event.model_id}' already has an adaptation job queued or running",
                    model_id=event.model_id,
                    running_job_id=previous_holder,
                )
            if take_over_expired_lock(session, event.model_id, job_id, settings.model_lock_ttl_s):
                if previous_holder and previous_holder != job_id:
                    _fail_abandoned_job(session, settings, previous_holder, job_id)
            else:
                add_lock(session, event.model_id, job_id, settings.model_lock_ttl_s)
                session.flush()
        except IntegrityError:
            # Lost a race: either another caller inserted the same idempotency_key first (a
            # duplicate), or another job holds this model's lock (busy).
            session.rollback()
            existing = session.execute(
                select(AdaptationJob).where(AdaptationJob.idempotency_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                return _to_response(existing, duplicate=True)
            holder = session.get(ModelLock, event.model_id)
            raise ModelBusyError(
                f"model '{event.model_id}' already has an adaptation job queued or running",
                model_id=event.model_id,
                running_job_id=holder.job_id if holder else None,
            ) from None
        session.add(
            AdaptationEvent(
                job_id=job_id,
                component="orchestrator",
                from_status=None,
                to_status=JobStatus.RECEIVED,
                message="job received",
            )
        )
        record_job_transition(
            session, settings, job_id=job_id, model_id=event.model_id, from_status=None,
            to_status=JobStatus.RECEIVED, message="job received",
        )
        record_audit(
            session,
            AuditAction.DRIFT_RECEIVED,
            component="orchestrator",
            actor=actor,
            job_id=job_id,
            model_id=event.model_id,
            reason="drift event received",
            metadata={"idempotency_key": key, "event": event.model_dump(mode="json")},
        )
        apply_transition(session, settings, job, JobStatus.QUEUED,
                         message=f"job queued for worker class '{job.worker_class}'")
        session.flush()
        response = _to_response(job, duplicate=False)

    log_event(logger, "job queued", adaptation_job_id=job_id, worker_class=response.worker_class)
    if queue.runs_inline:
        # orchestrator.worker builds on this module, so it is imported where it is needed.
        from oran_adapt.orchestrator.worker import Worker

        worker = Worker(session_factory, settings, registry=registry, llm_client=llm_client,
                        workdir=workdir, queue=queue)
        return worker.run_to_settled(job_id)
    publish(session_factory, queue, job_id)
    return response


def request_cancel(session_factory, settings: Settings, job_id: str, *,
                   actor: str) -> tuple[JobResponse, bool]:
    """Cancel a job. A queued job is recorded CANCELLED at once (True). A running one gets a
    cancel request (False): its worker stops the attempt at its next checkpoint - a supervisor
    tick every JOB_HEARTBEAT_S or a stage report - and records it CANCELLED. A job that ended,
    or is registering or promoting, cannot be cancelled (JobNotCancellableError)."""
    with session_scope(session_factory) as session:
        job = load_job(session, job_id, lock=True)
        status = JobStatus(job.status)
        if status in TERMINAL_STATUSES or status in _REGISTRY_STAGES:
            raise JobNotCancellableError(
                f"job '{job_id}' is {status.value} and cannot be cancelled",
                job_id=job_id, status=status.value,
            )
        if job.cancel_requested_at is None:
            job.cancel_requested_at = _now()
            job.cancel_requested_by = actor
        immediate = status == JobStatus.QUEUED and job.lease_token is None
        if immediate:
            error = JobCancelledError("the job was cancelled on request before it ran",
                                      job_id=job_id, requested_by=actor).to_dict()
            apply_transition(session, settings, job, JobStatus.CANCELLED,
                             message=error["message"], error=error)
            release_lock(session, job.model_id, job_id)
            session.execute(delete(JobSlot).where(JobSlot.job_id == job_id))
            metrics.JOB_CANCELLED.labels("queued").inc()
        record_audit(
            session, AuditAction.JOB_CANCEL_REQUESTED, component="orchestrator", actor=actor,
            job_id=job_id, model_id=job.model_id, reason="cancel requested",
            metadata={"status": status.value, "immediate": immediate},
        )
        session.flush()
        response = _to_response(job, duplicate=False)
    log_event(logger, "job cancel requested", adaptation_job_id=job_id, immediate=immediate)
    return response, immediate


def list_jobs(
    session: Session,
    *,
    limit: int,
    offset: int = 0,
    status: JobStatus | None = None,
    model_id: str | None = None,
    tenant: str | None = None,
    quarantined: bool | None = None,
) -> dict[str, Any]:
    """A page of jobs, newest first, with the total matching the filters."""
    filters = []
    if status is not None:
        filters.append(AdaptationJob.status == status.value)
    if model_id is not None:
        filters.append(AdaptationJob.model_id == model_id)
    if tenant is not None:
        filters.append(AdaptationJob.tenant == tenant)
    if quarantined is not None:
        filters.append(AdaptationJob.quarantined.is_(quarantined))
    total = session.scalar(select(func.count()).select_from(AdaptationJob).where(*filters))
    rows = session.scalars(
        select(AdaptationJob).where(*filters)
        .order_by(AdaptationJob.created_at.desc(), AdaptationJob.job_id)
        .limit(limit).offset(offset)
    )
    return {
        "items": [_to_response(job, duplicate=False).model_dump(mode="json") for job in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


def _attempt(payload: dict) -> dict:
    """One pipeline attempt, wherever the job executor runs it. Returns the JobResult as JSON.

    In a fresh worker process (``payload["in_process"]`` false) it first rebuilds what the
    parent process holds: logging, the correlation id and a database engine. Every stage it
    reports is fenced by the attempt's lease token."""
    settings: Settings = payload["settings"]
    engine = None
    if payload["in_process"]:
        factory = payload["session_factory"]
    else:
        configure_logging(settings.log_level, settings.log_json)
        set_correlation_id(payload["correlation_id"])
        engine = create_db_engine(payload["db_url"])
        factory = make_session_factory(engine)
    job_id = payload["job_id"]
    fence = payload["lease_token"]

    def _on_stage(status: JobStatus, message: str) -> None:
        _transition(factory, job_id, settings=settings, to_status=status, message=message,
                    fence=fence)

    try:
        current_job.set(JobContext(job_id=job_id, on_stage=_on_stage))
        with session_scope(factory) as session:
            result = payload["pipeline"](
                session,
                payload["event"],
                settings,
                registry=payload["registry"],
                llm_client=payload["llm_client"],
                workdir=payload["workdir"],
            )
        dumped: dict = result.model_dump(mode="json")
        return dumped
    finally:
        if engine is not None:
            engine.dispose()


def _run_once(
    event: DriftEvent,
    settings: Settings,
    *,
    executor: JobExecutorPort,
    registry: ModelRegistryPort | None,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    lease_token: str,
    timeout_s: float,
    on_tick: Callable[[], Exception | None] | None = None,
    on_abandoned: Callable[[], None] | None = None,
) -> JobResult:
    """One attempt, bounded by ``timeout_s``, on ``executor``, woken every JOB_HEARTBEAT_S to
    call ``on_tick``. Raises whatever the pipeline raised, JobTimeoutError, or what
    ``on_tick`` returned."""
    payload = {
        "event": event,
        "settings": settings,
        "workdir": workdir,
        "job_id": job_id,
        "lease_token": lease_token,
        "correlation_id": get_correlation_id(),
        "db_url": session_factory.kw["bind"].url.render_as_string(hide_password=False),
        "registry": registry,
        "llm_client": llm_client,
        # Looked up at call time, so a module-level stand-in patched onto this module is used.
        "pipeline": run_adaptation_job,
        "in_process": executor.in_process,
    }
    if executor.in_process:
        payload["session_factory"] = session_factory
    call = JobCall(
        job_id=job_id,
        timeout_s=timeout_s,
        run=_attempt,
        payload=payload,
        on_abandoned=on_abandoned,
        tick_s=settings.job_heartbeat_s,
        on_tick=on_tick,
    )
    return JobResult.model_validate(executor.execute(call))


def count_outcome(response: JobResponse) -> None:
    outcome = (response.result or {}).get("outcome") or "NONE"
    metrics.ADAPTATION_JOBS.labels(response.status.value, outcome).inc()
    if response.status == JobStatus.COMPLETED:
        metrics.ADAPTATION_SUCCESS.labels(outcome).inc()
    elif response.status == JobStatus.ROLLED_BACK:
        metrics.ADAPTATION_FAILURE.labels("ROLLED_BACK").inc()
    else:
        metrics.ADAPTATION_FAILURE.labels((response.error or {}).get("code", "UNKNOWN")).inc()


def timeout_error(last_stage: str, exc: JobTimeoutError) -> dict:
    """The recorded error of a job that ran past its timeout or deadline in ``last_stage``."""
    error = exc.to_dict()
    error["context"]["last_stage"] = last_stage
    metrics.JOB_TIMEOUTS.labels(str(last_stage)).inc()
    if last_stage in _REGISTRY_STAGES:
        error["context"]["needs_reconciliation"] = True
        log_event(
            logger, f"job timed out while {last_stage}: check the live alias",
            level=logging.WARNING, adaptation_job_id=exc.context.get("job_id"),
        )
    return error


def in_registry_stage(status: str) -> bool:
    return JobStatus(status) in _REGISTRY_STAGES
