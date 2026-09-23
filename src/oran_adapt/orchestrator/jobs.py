"""Phase 10 hardening: wraps the pure pipeline (`orchestrator.pipeline.run_adaptation_job`,
Phase 9) with everything a durable job needs that the pure function deliberately leaves out -
persistence, idempotency, concurrency-safe deduplication, retries on transient failures, and a
wall-clock timeout. `submit_adaptation_job` is the one function callers (the API route) use;
`run_adaptation_job` only reports its stages (orchestrator.context) and is otherwise ignorant
of all of this.

Idempotency and concurrency: every job is keyed by `DriftEvent.idempotency_key()`, enforced by a
unique DB constraint on `AdaptationJob.idempotency_key`. Two callers racing to submit the same
event either see each other's row on the initial lookup, or lose the INSERT race and catch the
resulting IntegrityError - either way, exactly one of them runs the pipeline and both get back
the same job's outcome (the loser with `duplicate=True`).

Retries: only `RegistryUnavailableError` and `DatabaseUnavailableError` are retried (transient,
infrastructure-level) - a deterministic failure like `ModelNotFoundError` or a rejected candidate
is never retried, since re-running would just fail (or reject) the same way again.

Locking: a job is inserted together with its model's lock (orchestrator.locks), so only one
job per model runs at a time. A different event for a model that is busy is refused with
ModelBusyError and nothing is recorded. The lock is released when the job ends; after a timeout
it is released only once the detached worker actually finishes, and the lock TTL is the backstop
if the process dies.

States: every transition goes through core.state_machine. The wrapper records RECEIVED ->
VALIDATING -> DATA_PREPARING; the pipeline reports the stages after that through
orchestrator.context, each recorded here as its own transition. A worker left running after a
timeout finds its job FAILED at its next stage report, which the state machine refuses, so it
stops there instead of going on to promote anything.

Timeout: the pipeline runs in a worker thread so the caller can bound how long it waits
(`Settings.job_timeout_s`) via `Future.result(timeout=...)`. Python has no way to forcibly kill a
running thread, so on a timeout the worker is left to finish on its own, using its own
independent DB session - `shutdown(wait=False)` only stops *this call* from blocking on it, it
does not stop the thread. This is the same honestly-documented limitation as the sandbox's
subprocess timeout (see `sandbox/runner.py`), one level up.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oran_adapt.core.config import Settings
from oran_adapt.core.enums import JobStatus
from oran_adapt.core.errors import (
    AdaptationError,
    DatabaseUnavailableError,
    JobTimeoutError,
    ModelBusyError,
    RegistryUnavailableError,
)
from oran_adapt.core.logging import log_event
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.core.state_machine import check_transition
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AdaptationEvent, AdaptationJob, ModelLock
from oran_adapt.llm.client import LlmClient
from oran_adapt.orchestrator.context import JobContext, current_job
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.registry.client import MlflowRegistry

logger = logging.getLogger(__name__)

_RETRYABLE = (RegistryUnavailableError, DatabaseUnavailableError)


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
    )


def _transition(
    session_factory,
    job_id: str,
    *,
    to_status: JobStatus,
    message: str = "",
    error: dict | None = None,
    result: dict | None = None,
    strategy: str | None = None,
) -> JobResponse:
    """Load the job in its own short transaction, record the state change as both the row's
    current status and an immutable `AdaptationEvent`, and commit - so a job's persisted status
    always reflects the last step that actually finished, even if the process dies right after."""
    with session_scope(session_factory) as session:
        job = session.execute(select(AdaptationJob).where(AdaptationJob.job_id == job_id)).scalar_one()
        check_transition(job.status, to_status)
        session.add(
            AdaptationEvent(
                job_id=job_id,
                component="orchestrator",
                from_status=job.status,
                to_status=to_status,
                message=message,
            )
        )
        job.status = to_status
        if error is not None:
            job.error = error
        if result is not None:
            job.result = result
        if strategy is not None:
            job.strategy = strategy
        session.flush()
        response = _to_response(job, duplicate=False)
    log_event(logger, message or f"job -> {to_status}", adaptation_job_id=job_id, status=to_status)
    return response


def _run_once(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    on_abandoned: Callable[[], None] | None = None,
) -> JobResult:
    """One attempt: runs the pure pipeline in a worker thread, on its own DB session, bounded by
    `settings.job_timeout_s`. Raises whatever the pipeline itself raised, or JobTimeoutError.
    On a timeout, ``on_abandoned`` runs once the detached worker has actually finished."""

    def _on_stage(status: JobStatus, message: str) -> None:
        _transition(session_factory, job_id, to_status=status, message=message)

    def _call() -> JobResult:
        current_job.set(JobContext(job_id=job_id, on_stage=_on_stage))
        with session_scope(session_factory) as worker_session:
            return run_adaptation_job(
                worker_session,
                event,
                settings,
                registry=registry,
                llm_client=llm_client,
                workdir=workdir,
            )

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_call)
    try:
        return future.result(timeout=settings.job_timeout_s)
    except FutureTimeoutError as exc:
        if on_abandoned is not None:
            future.add_done_callback(lambda _f: on_abandoned())
        raise JobTimeoutError(
            f"adaptation job exceeded its {settings.job_timeout_s:.0f}s timeout",
            timeout_s=settings.job_timeout_s,
        ) from exc
    finally:
        pool.shutdown(wait=False)


def _run_with_retries(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    on_abandoned: Callable[[], None] | None = None,
) -> JobResult:
    max_attempts = settings.job_max_retries + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_once(
                event,
                settings,
                registry=registry,
                llm_client=llm_client,
                workdir=workdir,
                session_factory=session_factory,
                job_id=job_id,
                on_abandoned=on_abandoned,
            )
        except _RETRYABLE as exc:
            if attempt >= max_attempts:
                raise
            backoff = settings.job_retry_backoff_s * (2 ** (attempt - 1))
            _transition(
                session_factory,
                job_id,
                to_status=JobStatus.DATA_PREPARING,
                message=f"retry {attempt}/{settings.job_max_retries} after {exc.code}: {exc.message}",
            )
            time.sleep(backoff)
    raise AssertionError("unreachable: loop above always returns or raises")


def submit_adaptation_job(
    session_factory,
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
) -> JobResponse:
    """Idempotent, retried, timed-out entry point for one drift event. Safe to call twice (or
    concurrently) with events that carry the same `idempotency_key()`: every call after the first
    returns the first call's outcome (or its current progress, if still running) with
    `duplicate=True`, instead of re-running the pipeline. Never raises for a pipeline-level
    failure - those are recorded as a FAILED job and returned as a JobResponse; only an
    infrastructure failure while recording the job itself (e.g. PostgreSQL unreachable) escapes
    as an exception, since there is nothing to record a job status into in that case."""
    key = event.idempotency_key()
    job_id = uuid.uuid4().hex

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
        )
        session.add(job)
        try:
            session.flush()
            if not take_over_expired_lock(
                session, event.model_id, job_id, settings.model_lock_ttl_s
            ):
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
                f"model '{event.model_id}' already has an adaptation job running",
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

    abandoned = threading.Event()

    def _release() -> None:
        try:
            with session_scope(session_factory) as session:
                release_lock(session, event.model_id, job_id)
        except Exception as exc:  # noqa: BLE001 - the TTL frees it; never mask the job outcome
            log_event(
                logger, f"could not release model lock: {exc}", level=logging.WARNING,
                adaptation_job_id=job_id,
            )

    def _on_abandoned() -> None:
        abandoned.set()
        _release()

    try:
        return _execute(
            event, settings, registry=registry, llm_client=llm_client,
            workdir=os.path.join(workdir, job_id), session_factory=session_factory,
            job_id=job_id, on_abandoned=_on_abandoned,
        )
    finally:
        if not abandoned.is_set():
            _release()


def _execute(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    on_abandoned: Callable[[], None],
) -> JobResponse:
    try:
        _transition(
            session_factory, job_id, to_status=JobStatus.VALIDATING, message="event accepted"
        )
        _transition(
            session_factory, job_id, to_status=JobStatus.DATA_PREPARING, message="pipeline started"
        )
        result = _run_with_retries(
            event,
            settings,
            registry=registry,
            llm_client=llm_client,
            workdir=workdir,
            session_factory=session_factory,
            job_id=job_id,
            on_abandoned=on_abandoned,
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: no pipeline failure escapes un-recorded
        error = (
            exc.to_dict()
            if isinstance(exc, AdaptationError)
            else {"code": "INTERNAL_ERROR", "message": str(exc)}
        )
        return _transition(
            session_factory, job_id, to_status=JobStatus.FAILED, message=str(exc), error=error
        )

    final = JobStatus.ROLLED_BACK if result.outcome == "ROLLED_BACK" else JobStatus.COMPLETED
    return _transition(
        session_factory,
        job_id,
        to_status=final,
        message=result.reason,
        result=result.model_dump(mode="json"),
        strategy=result.strategy.value if result.strategy else None,
    )
