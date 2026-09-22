"""Phase 10 hardening: wraps the pure pipeline (`orchestrator.pipeline.run_adaptation_job`,
Phase 9) with everything a durable job needs that the pure function deliberately leaves out -
persistence, idempotency, concurrency-safe deduplication, retries on transient failures, and a
wall-clock timeout. `submit_adaptation_job` is the one function callers (the API route) use;
`run_adaptation_job` itself stays untouched and ignorant of all of this.

Idempotency and concurrency: every job is keyed by `DriftEvent.idempotency_key()`, enforced by a
unique DB constraint on `AdaptationJob.idempotency_key`. Two callers racing to submit the same
event either see each other's row on the initial lookup, or lose the INSERT race and catch the
resulting IntegrityError - either way, exactly one of them runs the pipeline and both get back
the same job's outcome (the loser with `duplicate=True`).

Retries: only `RegistryUnavailableError` and `DatabaseUnavailableError` are retried (transient,
infrastructure-level) - a deterministic failure like `ModelNotFoundError` or a rejected candidate
is never retried, since re-running would just fail (or reject) the same way again.

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
import time
import uuid
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
    RegistryUnavailableError,
)
from oran_adapt.core.logging import log_event
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AdaptationEvent, AdaptationJob
from oran_adapt.llm.client import LlmClient
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
) -> JobResult:
    """One attempt: runs the pure pipeline in a worker thread, on its own DB session, bounded by
    `settings.job_timeout_s`. Raises whatever the pipeline itself raised, or JobTimeoutError."""

    def _call() -> JobResult:
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
            )
        except _RETRYABLE as exc:
            if attempt >= max_attempts:
                raise
            backoff = settings.job_retry_backoff_s * (2 ** (attempt - 1))
            _transition(
                session_factory,
                job_id,
                to_status=JobStatus.ANALYZING,
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
        except IntegrityError:
            # Lost the race: another caller inserted the same idempotency_key first.
            session.rollback()
            existing = session.execute(
                select(AdaptationJob).where(AdaptationJob.idempotency_key == key)
            ).scalar_one()
            return _to_response(existing, duplicate=True)
        session.add(
            AdaptationEvent(
                job_id=job_id,
                component="orchestrator",
                from_status=None,
                to_status=JobStatus.RECEIVED,
                message="job received",
            )
        )

    _transition(session_factory, job_id, to_status=JobStatus.ANALYZING, message="pipeline started")

    job_workdir = os.path.join(workdir, job_id)
    try:
        result = _run_with_retries(
            event,
            settings,
            registry=registry,
            llm_client=llm_client,
            workdir=job_workdir,
            session_factory=session_factory,
            job_id=job_id,
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

    return _transition(
        session_factory,
        job_id,
        to_status=JobStatus.COMPLETED,
        message=result.reason,
        result=result.model_dump(mode="json"),
        strategy=result.strategy.value if result.strategy else None,
    )
