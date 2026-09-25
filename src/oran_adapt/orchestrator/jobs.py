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

Timeout (`Settings.job_timeout_s`), by `Settings.job_execution_mode`:
- "process" (default): each attempt runs in a fresh worker process (multiprocessing "spawn"). The
  child rebuilds its own DB engine, MLflow registry and LLM client from picklable settings and
  records stage transitions itself. On a timeout the parent terminates it, then kills it if it
  has not exited within a grace period. On POSIX the worker leads its own process group, so the
  sandbox subprocesses it started die with it. On Windows only the worker itself is killed: a
  sandbox subprocess it had started runs until its own work ends. A worker killed while
  REGISTERING or PROMOTING may leave the registry half-updated; the TIMED_OUT error then carries
  ``needs_reconciliation`` so an operator checks the live alias.
- "thread": the pipeline runs in a worker thread of this process. Python cannot kill a thread,
  so on a timeout it is left to finish; its next stage report finds the job TIMED_OUT, which the
  state machine refuses, so it stops there instead of promoting anything. Only for tests and
  debugging that inject in-process fakes.
"""

from __future__ import annotations

import contextvars
import json
import logging
import multiprocessing
import os
import pickle  # nosec B403 - only to check that parent-built job inputs can reach the worker
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from multiprocessing.connection import Connection

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oran_adapt.core import metrics
from oran_adapt.core.audit import record_audit
from oran_adapt.core.config import Settings
from oran_adapt.core.correlation import get_correlation_id, set_correlation_id
from oran_adapt.core.enums import TERMINAL_STATUSES, AuditAction, JobStatus
from oran_adapt.core.errors import (
    AdaptationError,
    ConfigurationError,
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
from oran_adapt.llm.client import (
    AnthropicLlmClient,
    GeminiLlmClient,
    InstrumentedLlmClient,
    LlmClient,
    build_llm_client,
)
from oran_adapt.orchestrator.context import JobContext, current_job
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.registry.client import MlflowRegistry

logger = logging.getLogger(__name__)

_RETRYABLE = (RegistryUnavailableError, DatabaseUnavailableError)

# How long a terminated worker gets to exit before it is killed outright.
_KILL_GRACE_S = 5.0
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
    """One attempt, bounded by `settings.job_timeout_s`, in a worker process or thread (see the
    module docstring). Raises whatever the pipeline itself raised, or JobTimeoutError."""
    if settings.job_execution_mode == "process":
        return _run_in_process(
            event, settings, registry=registry, llm_client=llm_client, workdir=workdir,
            session_factory=session_factory, job_id=job_id,
        )
    return _run_in_thread(
        event, settings, registry=registry, llm_client=llm_client, workdir=workdir,
        session_factory=session_factory, job_id=job_id, on_abandoned=on_abandoned,
    )


def _run_in_thread(
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
    """On a timeout, ``on_abandoned`` runs once the detached worker thread has finished."""

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

    # The worker runs in a copy of this context, so the request's correlation id reaches the
    # pipeline's logs and audit rows.
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ctx.run, _call)
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


# ---- process mode ---------------------------------------------------------------------------
def _portable_registry(registry: object) -> tuple:
    # An MlflowRegistry holds an MlflowClient, which is rebuilt in the worker from its URIs.
    if isinstance(registry, MlflowRegistry):
        return ("mlflow", registry.tracking_uri, registry.registry_uri, registry.skops_trusted_types)
    return ("object", registry)


def _portable_llm(client: LlmClient | None) -> tuple:
    # Provider SDK clients are rebuilt in the worker from settings rather than pickled.
    if isinstance(client, InstrumentedLlmClient) and isinstance(
        client.inner, AnthropicLlmClient | GeminiLlmClient
    ):
        return ("settings",)
    return ("object", client)


def _plain(context: dict) -> dict:
    """``context`` reduced to JSON-safe values, so it always survives the trip to the parent."""
    return json.loads(json.dumps(context, default=str))


def _worker_main(conn: Connection, payload: dict) -> None:
    """Entry point of a job's worker process. Sends back exactly one message:
    ("ok", deltas, result) | ("error", deltas, code, message, context) | ("crash", deltas, text),
    where deltas is how far this worker moved the forwarded metrics (metrics.delta_since)."""
    if sys.platform != "win32":
        os.setpgrp()  # lead a process group, so a timeout kills sandbox subprocesses too
    engine = None
    before = metrics.snapshot()
    try:
        settings: Settings = payload["settings"]
        configure_logging(settings.log_level, settings.log_json)
        set_correlation_id(payload["correlation_id"])
        engine = create_db_engine(payload["db_url"])
        factory = make_session_factory(engine)
        job_id = payload["job_id"]

        def _on_stage(status: JobStatus, message: str) -> None:
            _transition(factory, job_id, to_status=status, message=message)

        current_job.set(JobContext(job_id=job_id, on_stage=_on_stage))

        kind, *spec = payload["registry"]
        registry = (
            MlflowRegistry(spec[0], spec[1], skops_trusted_types=spec[2])
            if kind == "mlflow"
            else spec[0]
        )
        kind, *spec = payload["llm_client"]
        llm_client = build_llm_client(settings) if kind == "settings" else spec[0]

        with session_scope(factory) as session:
            result = payload["pipeline"](
                session,
                payload["event"],
                settings,
                registry=registry,
                llm_client=llm_client,
                workdir=payload["workdir"],
            )
        conn.send(("ok", metrics.delta_since(before), result.model_dump(mode="json")))
    except AdaptationError as exc:
        deltas = metrics.delta_since(before)
        conn.send(("error", deltas, exc.code, exc.message, _plain(exc.context)))
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which records it
        conn.send(("crash", metrics.delta_since(before), f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()
        if engine is not None:
            engine.dispose()


def _rebuild_error(code: str, message: str, context: dict) -> AdaptationError:
    """The worker's AdaptationError, rebuilt in the parent with its class, so retry decisions
    (``_RETRYABLE``) and the recorded error code are the same as in thread mode."""
    stack: list[type[AdaptationError]] = [AdaptationError]
    while stack:
        cls = stack.pop()
        if cls.code == code:
            try:
                return cls(message, **context)
            except TypeError:
                break
        stack.extend(cls.__subclasses__())
    err = AdaptationError(message, **context)
    err.code = code
    return err


def _stop_worker(proc: multiprocessing.process.BaseProcess) -> None:
    """Terminate the worker (and, on POSIX, its process group), then kill it if it is still
    alive after the grace period. Only ever touches the process this module started."""
    pid = proc.pid
    if sys.platform != "win32" and pid is not None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()  # the worker had not become a group leader yet
    else:
        proc.terminate()
    proc.join(_KILL_GRACE_S)
    if proc.is_alive():
        if sys.platform != "win32" and pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.kill()
        proc.join(_KILL_GRACE_S)


def _run_in_process(
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
    session_factory,
    job_id: str,
) -> JobResult:
    payload = {
        "event": event,
        "settings": settings,
        "workdir": workdir,
        "job_id": job_id,
        "correlation_id": get_correlation_id(),
        "db_url": session_factory.kw["bind"].url.render_as_string(hide_password=False),
        "registry": _portable_registry(registry),
        "llm_client": _portable_llm(llm_client),
        # Looked up at call time, so a module-level stand-in patched onto this module is used.
        "pipeline": run_adaptation_job,
    }
    try:
        pickle.dumps(payload)
    except Exception as exc:
        raise ConfigurationError(
            "job inputs cannot be sent to a worker process",
            cause=str(exc),
            hint="in-process fakes need JOB_EXECUTION_MODE=thread",
        ) from exc

    mp = multiprocessing.get_context("spawn")
    recv_end, send_end = mp.Pipe(duplex=False)
    proc = mp.Process(target=_worker_main, args=(send_end, payload), name=f"oran-job-{job_id[:8]}")
    proc.start()
    send_end.close()  # only the child writes; EOF then means the child is gone
    try:
        if not recv_end.poll(settings.job_timeout_s):
            _stop_worker(proc)
            raise JobTimeoutError(
                f"adaptation job exceeded its {settings.job_timeout_s:.0f}s timeout",
                timeout_s=settings.job_timeout_s,
                worker_pid=proc.pid,
            )
        try:
            message = recv_end.recv()
        except EOFError:
            message = None
    finally:
        recv_end.close()
        proc.join(_KILL_GRACE_S)
        if proc.is_alive():
            _stop_worker(proc)

    if message is None:
        raise RuntimeError(f"job worker process exited with code {proc.exitcode} and no result")
    kind, deltas, *rest = message
    metrics.apply_delta(deltas)
    if kind == "ok":
        return JobResult.model_validate(rest[0])
    if kind == "error":
        raise _rebuild_error(*rest)
    raise RuntimeError(rest[0])


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
    registry: MlflowRegistry,
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
