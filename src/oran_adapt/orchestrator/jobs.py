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
it is released once the worker is dead (process mode) or has finished (thread mode), and the
lock TTL is the backstop if this process dies.

States: every transition goes through core.state_machine. The wrapper records RECEIVED ->
VALIDATING -> DATA_PREPARING; the pipeline reports the stages after that through
orchestrator.context, each recorded as its own transition. A job that runs past its timeout ends
TIMED_OUT, a terminal state.

Timeout (`Settings.job_timeout_s`): each attempt runs on the JobExecutorPort adapter that
`Settings.job_execution_mode` names, resolved through the composition root (oran_adapt.bootstrap)
on every attempt, so a per-call `settings.model_copy(...)` picks its own executor.
- "process" (default, adapters.job_executors): each attempt runs in a fresh worker process. The
  child rebuilds its own DB engine from the URL; the registry and LLM adapters pickle themselves
  and rebuild their clients. On a timeout the parent terminates the worker, then kills it after
  JOB_KILL_GRACE_S. On POSIX the worker leads its own process group, so the sandbox subprocesses
  it started die with it. On Windows only the worker itself is killed: a sandbox subprocess it
  had started runs until its own work ends. A worker killed while REGISTERING or PROMOTING may
  leave the registry half-updated; the TIMED_OUT error then carries ``needs_reconciliation`` so
  an operator checks the live alias.
- "thread": the pipeline runs in a worker thread of this process. Python cannot kill a thread,
  so on a timeout it is left to finish; its next stage report finds the job TIMED_OUT, which the
  state machine refuses, so it stops there instead of promoting anything. Only for tests and
  debugging that inject in-process fakes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oran_adapt.bootstrap import build_job_executor
from oran_adapt.core import metrics
from oran_adapt.core.audit import record_audit
from oran_adapt.core.config import Settings
from oran_adapt.core.correlation import get_correlation_id, set_correlation_id
from oran_adapt.core.enums import TERMINAL_STATUSES, AuditAction, JobStatus
from oran_adapt.core.errors import (
    AdaptationError,
    DatabaseUnavailableError,
    JobAbandonedError,
    JobTimeoutError,
    ModelBusyError,
    RegistryUnavailableError,
)
from oran_adapt.core.logging import configure_logging, log_event
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.core.state_machine import check_transition
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import AdaptationEvent, AdaptationJob, ModelLock
from oran_adapt.llm.client import LlmClient
from oran_adapt.orchestrator.context import JobContext, current_job
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.ports import JobCall, ModelRegistryPort

logger = logging.getLogger(__name__)

_RETRYABLE = (RegistryUnavailableError, DatabaseUnavailableError)

# Stages where a killed worker may have changed the registry without finishing.
_REGISTRY_STAGES = frozenset({JobStatus.REGISTERING, JobStatus.PROMOTING})


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


def _fail_abandoned_job(session, old_job_id: str, taken_over_by: str) -> None:
    """The lock of ``old_job_id`` expired and ``taken_over_by`` now holds it, so the process that
    ran the old job died (a crash or a service restart). Record the old job FAILED in the
    takeover's transaction so it never stays in a running state; should its worker still be
    alive, its next stage report is refused by the state machine, as after a timeout."""
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
    check_transition(last_stage, JobStatus.FAILED)
    session.add(
        AdaptationEvent(
            job_id=old_job_id,
            component="orchestrator",
            from_status=last_stage,
            to_status=JobStatus.FAILED,
            message=error["message"],
        )
    )
    old.status = JobStatus.FAILED
    old.error = error
    metrics.ADAPTATION_JOBS.labels(JobStatus.FAILED.value, "NONE").inc()
    metrics.ADAPTATION_FAILURE.labels(JobAbandonedError.code).inc()
    log_event(
        logger,
        f"job abandoned while {last_stage.value}; marked FAILED",
        level=logging.WARNING,
        adaptation_job_id=old_job_id,
    )


def _attempt(payload: dict) -> dict:
    """One pipeline attempt, wherever the job executor runs it. Returns the JobResult as JSON.

    In a fresh worker process (``payload["in_process"]`` false) it first rebuilds what the
    parent process holds: logging, the correlation id and a database engine."""
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

    def _on_stage(status: JobStatus, message: str) -> None:
        _transition(factory, job_id, to_status=status, message=message)

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
    registry: ModelRegistryPort | None,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    on_abandoned: Callable[[], None] | None = None,
) -> JobResult:
    """One attempt, bounded by `settings.job_timeout_s`, on the job executor JOB_EXECUTION_MODE
    names (see the module docstring). Raises whatever the pipeline raised, or JobTimeoutError."""
    executor = build_job_executor(settings)
    payload = {
        "event": event,
        "settings": settings,
        "workdir": workdir,
        "job_id": job_id,
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
        timeout_s=settings.job_timeout_s,
        run=_attempt,
        payload=payload,
        on_abandoned=on_abandoned,
    )
    return JobResult.model_validate(executor.execute(call))


def _run_with_retries(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: ModelRegistryPort | None,
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
    registry: ModelRegistryPort | None,
    llm_client: LlmClient | None,
    workdir: str,
    actor: str = "system",
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
            correlation_id=get_correlation_id(),
        )
        session.add(job)
        try:
            session.flush()
            previous_holder = session.scalar(
                select(ModelLock.job_id).where(ModelLock.model_id == event.model_id)
            )
            if take_over_expired_lock(session, event.model_id, job_id, settings.model_lock_ttl_s):
                if previous_holder and previous_holder != job_id:
                    _fail_abandoned_job(session, previous_holder, job_id)
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
    registry: ModelRegistryPort | None,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
    on_abandoned: Callable[[], None],
) -> JobResponse:
    started = time.perf_counter()
    metrics.ADAPTATION_IN_PROGRESS.inc()
    try:
        response = _execute_inner(
            event, settings, registry=registry, llm_client=llm_client, workdir=workdir,
            session_factory=session_factory, job_id=job_id, on_abandoned=on_abandoned,
        )
    finally:
        metrics.ADAPTATION_IN_PROGRESS.dec()
        metrics.ADAPTATION_DURATION.observe(time.perf_counter() - started)
    _count_outcome(response)
    return response


def _count_outcome(response: JobResponse) -> None:
    outcome = (response.result or {}).get("outcome") or "NONE"
    metrics.ADAPTATION_JOBS.labels(response.status.value, outcome).inc()
    if response.status == JobStatus.COMPLETED:
        metrics.ADAPTATION_SUCCESS.labels(outcome).inc()
    elif response.status == JobStatus.ROLLED_BACK:
        metrics.ADAPTATION_FAILURE.labels("ROLLED_BACK").inc()
    else:
        metrics.ADAPTATION_FAILURE.labels((response.error or {}).get("code", "UNKNOWN")).inc()


def _record_timeout(session_factory, job_id: str, exc: JobTimeoutError) -> JobResponse:
    with session_scope(session_factory) as session:
        last_stage = session.execute(
            select(AdaptationJob.status).where(AdaptationJob.job_id == job_id)
        ).scalar_one()
    error = exc.to_dict()
    error["context"]["last_stage"] = last_stage
    metrics.JOB_TIMEOUTS.labels(str(last_stage)).inc()
    if last_stage in _REGISTRY_STAGES:
        error["context"]["needs_reconciliation"] = True
        log_event(
            logger, f"job timed out while {last_stage}: check the live alias",
            level=logging.WARNING, adaptation_job_id=job_id,
        )
    return _transition(
        session_factory, job_id, to_status=JobStatus.TIMED_OUT, message=str(exc), error=error
    )


def _execute_inner(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: ModelRegistryPort | None,
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
    except JobTimeoutError as exc:
        return _record_timeout(session_factory, job_id, exc)
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
