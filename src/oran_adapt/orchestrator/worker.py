"""Job workers: claim queued jobs, run them under a lease, record every outcome exactly once.

A worker (``oran-adapt worker run``) serves one or more worker classes. It repeatedly

1. runs the reaper every JOB_REAP_INTERVAL_S (``reap``): jobs whose lease expired go back to
   the queue (or are quarantined), queued jobs past their deadline end TIMED_OUT, and queued
   jobs no broker message has reached are published again;
2. claims the next job (``claim``): the highest priority, oldest QUEUED job of its classes that
   is available, not quarantined and whose tenant has a free concurrency slot. The claim is a
   conditional UPDATE that only one worker can win; it sets a fresh lease token, the owner
   (``host:pid:nonce``) and the lease expiry, and counts the attempt;
3. runs one attempt on the job executor (JOB_EXECUTION_MODE) with a supervisor that wakes every
   JOB_HEARTBEAT_S (the checkpoint interval) to renew the lease, the model lock and the tenant
   slot, and to stop the attempt on a cancel request, the job deadline, a lost lease or a drain
   that ran past JOB_DRAIN_TIMEOUT_S;
4. records the outcome, fenced by the lease token (orchestrator.jobs.check_fence):

   ========================  ===========================================================
   the attempt               the job
   ========================  ===========================================================
   returned a result         COMPLETED or ROLLED_BACK
   ran past its timeout      TIMED_OUT (the whole worker process tree is killed)
   was cancelled             CANCELLED
   hit a transient error     QUEUED again after JOB_RETRY_BACKOFF_S * 2^(n-1) while retries
                             remain (JOB_MAX_RETRIES), else FAILED
   lost its worker process   QUEUED again; quarantined (FAILED, JOB_QUARANTINED) after
                             JOB_POISON_THRESHOLD such attempts; FAILED with
                             needs_reconciliation when lost while registering or promoting
   was drained               QUEUED again, the attempt not counted against the retries
   lost its lease            nothing: the lease's new holder (the reaper) decides
   failed otherwise          FAILED
   ========================  ===========================================================

A job is taken to an outcome at most once: every write of an attempt is fenced by its token,
and the reaper takes a lease over only after it expired, with a conditional update on the old
token. A worker killed mid-job leaves an expiring lease, which the reaper turns into a requeue:
the job is never stuck and never run to an outcome twice.

Brokers (JOB_QUEUE_BACKEND) only wake workers: a message carries the job id and the worker it
reaches calls ``run_job_by_id``, which claims that job like any other claim. The ``inline``
backend runs a job in the submitting call (``Worker.run_to_settled``).

Graceful drain: ``Worker.drain()`` (SIGTERM, SIGINT, or Ctrl+Break on Windows) stops claiming,
lets the running job continue for JOB_DRAIN_TIMEOUT_S, then stops it and puts it back in the
queue for another worker.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from oran_adapt.bootstrap import (
    build_job_executor,
    build_job_queue,
    build_llm,
    build_registry,
)
from oran_adapt.core import metrics
from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.enums import TERMINAL_STATUSES, JobStatus
from oran_adapt.core.errors import (
    AdaptationError,
    DatabaseUnavailableError,
    InvalidTransitionError,
    JobAbandonedError,
    JobCancelledError,
    JobDrainedError,
    JobLeaseLostError,
    JobQuarantinedError,
    JobQueueUnavailableError,
    JobTimeoutError,
    JobWorkerLostError,
    RegistryUnavailableError,
)
from oran_adapt.core.logging import log_event
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import AdaptationJob, JobSlot, ModelLock
from oran_adapt.orchestrator import jobs
from oran_adapt.orchestrator.locks import release_lock

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

    from oran_adapt.llm.client import LlmClient
    from oran_adapt.ports import JobQueuePort, ModelRegistryPort

logger = logging.getLogger(__name__)

_RETRYABLE = (RegistryUnavailableError, DatabaseUnavailableError)
_ACTIVE = [s.value for s in JobStatus if s not in TERMINAL_STATUSES and s != JobStatus.QUEUED]


def _now() -> datetime:
    return datetime.now(UTC)


def new_owner() -> str:
    """``host:pid:nonce``: which process holds a lease (the pid is the real interpreter's)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Claim:
    """A job this worker holds the lease of, for one attempt."""

    job_id: str
    lease_token: str
    attempt: int
    tenant: str
    slot: int | None
    model_id: str
    event: dict[str, Any]
    deadline_at: datetime | None


class _NoSlot(Exception):
    """The tenant is at its concurrency limit (or lost the slot race): try the next job."""


def _cursor(result: Any) -> CursorResult:
    return cast(CursorResult, result)


def _take_slot(session: Session, settings: Settings, tenant: str, job_id: str,
               expires_at: datetime) -> int | None:
    """A concurrency slot for ``tenant`` (JOB_TENANT_LIMITS, else JOB_TENANT_CONCURRENCY;
    0 = unlimited, no slot). Raises _NoSlot when all are held."""
    limit = settings.job_tenant_limits.get(tenant, settings.job_tenant_concurrency)
    if limit <= 0:
        return None
    session.execute(delete(JobSlot).where(JobSlot.tenant == tenant,
                                          JobSlot.expires_at < _now()))
    used = set(session.scalars(select(JobSlot.slot).where(JobSlot.tenant == tenant)))
    free = next((n for n in range(limit) if n not in used), None)
    if free is None:
        raise _NoSlot
    session.add(JobSlot(tenant=tenant, slot=free, job_id=job_id, expires_at=expires_at))
    try:
        session.flush()
    except IntegrityError as exc:
        raise _NoSlot from exc
    return free


def _try_claim(session_factory, settings: Settings, job_id: str, owner: str) -> Claim | None:
    now = _now()
    token = uuid.uuid4().hex
    expires = now + timedelta(seconds=settings.job_lease_ttl_s)
    try:
        with session_scope(session_factory) as session:
            won = _cursor(session.execute(
                update(AdaptationJob)
                .where(
                    AdaptationJob.job_id == job_id,
                    AdaptationJob.status == JobStatus.QUEUED.value,
                    AdaptationJob.lease_token.is_(None),
                    AdaptationJob.quarantined.is_(False),
                )
                .values(lease_token=token, lease_owner=owner, lease_expires_at=expires,
                        attempt=AdaptationJob.attempt + 1)
            )).rowcount == 1
            if not won:
                return None
            job = jobs.load_job(session, job_id)
            slot = _take_slot(session, settings, job.tenant, job_id, expires)
            jobs.apply_transition(session, settings, job, JobStatus.VALIDATING,
                                  message=f"event accepted, attempt {job.attempt}")
            jobs.apply_transition(session, settings, job, JobStatus.DATA_PREPARING,
                                  message="pipeline started")
            session.execute(
                update(ModelLock)
                .where(ModelLock.model_id == job.model_id, ModelLock.job_id == job_id)
                .values(expires_at=now + timedelta(seconds=settings.model_lock_ttl_s))
            )
            claim = Claim(job_id=job_id, lease_token=token, attempt=job.attempt,
                          tenant=job.tenant, slot=slot, model_id=job.model_id,
                          event=dict(job.event), deadline_at=jobs.aware(job.deadline_at))
    except _NoSlot:
        return None
    log_event(logger, f"job claimed, attempt {claim.attempt}", adaptation_job_id=job_id,
              lease_owner=owner)
    return claim


def claim(session_factory, settings: Settings, *, owner: str,
          classes: Iterable[str] | None = None, job_id: str | None = None) -> Claim | None:
    """Claim one job: ``job_id`` when given, else the next claimable job of ``classes``
    (highest priority first, then oldest). None when there is nothing this worker may run."""
    if job_id is not None:
        return _try_claim(session_factory, settings, job_id, owner)
    now = _now()
    with session_scope(session_factory) as session:
        candidates = list(session.scalars(
            select(AdaptationJob.job_id)
            .where(
                AdaptationJob.status == JobStatus.QUEUED.value,
                AdaptationJob.lease_token.is_(None),
                AdaptationJob.quarantined.is_(False),
                AdaptationJob.worker_class.in_(list(classes or settings.job_worker_classes)),
                or_(AdaptationJob.available_at.is_(None), AdaptationJob.available_at <= now),
            )
            .order_by(AdaptationJob.priority.desc(), AdaptationJob.created_at,
                      AdaptationJob.job_id)
            .limit(settings.job_claim_candidates)
        ))
    for candidate in candidates:
        won = _try_claim(session_factory, settings, candidate, owner)
        if won is not None:
            return won
    return None


@dataclass
class _Supervisor:
    """The ``on_tick`` of an attempt: renews the lease, model lock and slot, and says when to
    stop the attempt (cancel requested, lease lost, drain timed out)."""

    session_factory: Any
    settings: Settings
    claim: Claim
    worker: Worker
    renewed_until: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        self.renewed_until = _now() + timedelta(seconds=self.settings.job_lease_ttl_s)

    def __call__(self) -> Exception | None:
        now = _now()
        c = self.claim
        try:
            with session_scope(self.session_factory) as session:
                held = _cursor(session.execute(
                    update(AdaptationJob)
                    .where(AdaptationJob.job_id == c.job_id,
                           AdaptationJob.lease_token == c.lease_token)
                    .values(lease_expires_at=now + timedelta(
                        seconds=self.settings.job_lease_ttl_s))
                )).rowcount == 1
                if not held:
                    return JobLeaseLostError("the job's lease passed to another worker",
                                             job_id=c.job_id)
                status, cancel_at = session.execute(
                    select(AdaptationJob.status, AdaptationJob.cancel_requested_at)
                    .where(AdaptationJob.job_id == c.job_id)
                ).one()
                session.execute(
                    update(ModelLock)
                    .where(ModelLock.model_id == c.model_id, ModelLock.job_id == c.job_id)
                    .values(expires_at=now + timedelta(seconds=self.settings.model_lock_ttl_s))
                )
                if c.slot is not None:
                    session.execute(
                        update(JobSlot)
                        .where(JobSlot.tenant == c.tenant, JobSlot.slot == c.slot,
                               JobSlot.job_id == c.job_id)
                        .values(expires_at=now + timedelta(seconds=self.settings.job_lease_ttl_s))
                    )
            self.renewed_until = now + timedelta(seconds=self.settings.job_lease_ttl_s)
        except SQLAlchemyError as exc:
            log_event(logger, f"could not renew the job's lease: {exc}", level=logging.WARNING,
                      adaptation_job_id=c.job_id)
            if now >= self.renewed_until:
                return JobLeaseLostError("the job's lease expired while the database was "
                                         "unreachable", job_id=c.job_id)
            return None
        if cancel_at is not None and not jobs.in_registry_stage(status):
            return JobCancelledError("the job was cancelled on request", job_id=c.job_id,
                                     stage=status)
        if self.worker.drain_expired():
            return JobDrainedError("the worker is shutting down and the job did not finish "
                                   "within JOB_DRAIN_TIMEOUT_S", job_id=c.job_id,
                                   drain_timeout_s=self.settings.job_drain_timeout_s)
        return None


def _end_attempt(session: Session, job: AdaptationJob, claim_: Claim, *,
                 release_model_lock: bool) -> None:
    """Clear the lease and free the slot (and the model lock when the job ended)."""
    job.lease_token = None
    job.lease_expires_at = None
    if claim_.slot is not None:
        session.execute(delete(JobSlot).where(JobSlot.tenant == claim_.tenant,
                                              JobSlot.slot == claim_.slot,
                                              JobSlot.job_id == claim_.job_id))
    if release_model_lock:
        release_lock(session, job.model_id, job.job_id)


def requeue(session: Session, settings: Settings, job: AdaptationJob, *, message: str,
            reason: str, delay_s: float = 0.0, charge: bool = True, lost: bool = False) -> None:
    """Put ``job`` back in the queue, available after ``delay_s``. ``charge`` False does not
    count the attempt; ``lost`` counts it towards JOB_POISON_THRESHOLD."""
    jobs.apply_transition(session, settings, job, JobStatus.QUEUED, message=message)
    job.available_at = _now() + timedelta(seconds=delay_s)
    job.published_at = None
    if not charge:
        job.attempt = max(0, job.attempt - 1)
    if lost:
        job.lost_count = (job.lost_count or 0) + 1
    metrics.JOB_REQUEUES.labels(reason).inc()


def lose(session: Session, settings: Settings, job: AdaptationJob, *, why: str,
         reason: str) -> bool:
    """An attempt of ``job`` ended without an outcome (its worker died or its lease expired).
    Requeue it, or end it: FAILED for reconciliation when lost while registering or promoting,
    quarantined once JOB_POISON_THRESHOLD attempts were lost. True when the job ended."""
    last_stage = job.status
    if jobs.in_registry_stage(last_stage):
        error = JobAbandonedError(f"{why} while {last_stage}", last_stage=last_stage,
                                  needs_reconciliation=True).to_dict()
        jobs.apply_transition(session, settings, job, JobStatus.FAILED,
                              message=error["message"], error=error)
        return True
    if (job.lost_count or 0) + 1 >= settings.job_poison_threshold:
        job.lost_count = (job.lost_count or 0) + 1
        job.quarantined = True
        error = JobQuarantinedError(
            f"{why}; {job.lost_count} attempts ended without an outcome, so the job is "
            "quarantined and not run again",
            last_stage=last_stage, lost_attempts=job.lost_count,
            threshold=settings.job_poison_threshold,
        ).to_dict()
        jobs.apply_transition(session, settings, job, JobStatus.FAILED,
                              message=error["message"], error=error)
        metrics.JOB_QUARANTINED.inc()
        log_event(logger, "poison job quarantined", level=logging.WARNING,
                  adaptation_job_id=job.job_id)
        return True
    backoff = settings.job_retry_backoff_s * (2 ** (job.lost_count or 0))
    requeue(session, settings, job, message=f"requeued after {why} in {last_stage}",
            reason=reason, delay_s=backoff, lost=True)
    return False


class Worker:
    """Claims and runs jobs (module docstring). ``registry``, ``llm_client`` and ``workdir``
    are handed to each attempt; ``classes`` defaults to JOB_WORKER_CLASSES."""

    def __init__(
        self,
        session_factory,
        settings: Settings,
        *,
        registry: ModelRegistryPort | None,
        llm_client: LlmClient | None,
        workdir: str,
        queue: JobQueuePort | None = None,
        classes: Iterable[str] | None = None,
        owner: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.registry = registry
        self.llm_client = llm_client
        self.workdir = workdir
        self.queue = queue
        self.classes = list(classes or settings.job_worker_classes)
        self.owner = owner or new_owner()
        self._draining = threading.Event()
        self._drain_deadline: float | None = None

    # -- drain ---------------------------------------------------------------------------------

    def drain(self) -> None:
        """Stop claiming; the running job gets JOB_DRAIN_TIMEOUT_S to finish."""
        if not self._draining.is_set():
            self._drain_deadline = time.monotonic() + self.settings.job_drain_timeout_s
            self._draining.set()
            log_event(logger, "worker draining", lease_owner=self.owner)

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def drain_expired(self) -> bool:
        return self._drain_deadline is not None and time.monotonic() >= self._drain_deadline

    # -- running -------------------------------------------------------------------------------

    def run(self, *, once: bool = False, max_jobs: int | None = None) -> int:
        """Reap, claim and run jobs until drained (``once``: until nothing is claimable;
        ``max_jobs``: at most that many). Returns how many jobs it ran."""
        ran = 0
        last_reap: float | None = None
        while not self.draining:
            if last_reap is None or time.monotonic() - last_reap >= self.settings.job_reap_interval_s:
                self.reap()
                last_reap = time.monotonic()
            claimed = claim(self.session_factory, self.settings, owner=self.owner,
                            classes=self.classes)
            if claimed is None:
                if once:
                    break
                self._draining.wait(self.settings.job_poll_interval_s)
                continue
            self.run_claim(claimed)
            ran += 1
            if max_jobs is not None and ran >= max_jobs:
                break
        return ran

    def reap(self) -> dict[str, int]:
        try:
            return reap(self.session_factory, self.settings, self._queue())
        except SQLAlchemyError as exc:
            log_event(logger, f"reaper could not reach the database: {exc}",
                      level=logging.WARNING)
            return {}

    def _queue(self) -> JobQueuePort:
        if self.queue is None:
            self.queue = build_job_queue(self.settings)
        return self.queue

    def run_job(self, job_id: str) -> bool:
        """Claim and run one attempt of ``job_id`` (a broker message). False when it could not
        be claimed: not queued, not yet available, claimed elsewhere or its tenant is full."""
        claimed = claim(self.session_factory, self.settings, owner=self.owner, job_id=job_id)
        if claimed is None:
            _unpublish(self.session_factory, job_id)
            return False
        self.run_claim(claimed)
        return True

    def run_to_settled(self, job_id: str) -> JobResponse:
        """Run ``job_id`` here until it ends (the ``inline`` backend): attempt after attempt,
        waiting out each retry backoff. Returns the ended job."""
        while True:
            with session_scope(self.session_factory) as session:
                job = jobs.load_job(session, job_id)
                status = JobStatus(job.status)
                available = jobs.aware(job.available_at)
                if status in TERMINAL_STATUSES:
                    return jobs._to_response(job, duplicate=False)
            if status == JobStatus.QUEUED and (available is None or available <= _now()):
                claimed = claim(self.session_factory, self.settings, owner=self.owner,
                                job_id=job_id)
                if claimed is not None:
                    self.run_claim(claimed)
                    continue
            wait = self.settings.job_poll_interval_s
            if status == JobStatus.QUEUED and available is not None:
                wait = max(0.0, min(wait, (available - _now()).total_seconds()))
            time.sleep(wait)

    def run_claim(self, claim_: Claim) -> None:
        """Run one attempt of a claimed job and record its outcome (module docstring)."""
        started = time.perf_counter()
        metrics.ADAPTATION_IN_PROGRESS.inc()
        try:
            self._run_claim(claim_)
        finally:
            metrics.ADAPTATION_IN_PROGRESS.dec()
            metrics.ADAPTATION_DURATION.observe(time.perf_counter() - started)

    def _timeout_s(self, claim_: Claim) -> float:
        timeout = self.settings.job_timeout_s
        if claim_.deadline_at is not None:
            timeout = min(timeout, (claim_.deadline_at - _now()).total_seconds())
        return max(timeout, 0.0)

    def _run_claim(self, claim_: Claim) -> None:
        executor = build_job_executor(self.settings)
        abandoned = threading.Event()

        def _release() -> None:
            try:
                with session_scope(self.session_factory) as session:
                    release_lock(session, claim_.model_id, claim_.job_id)
            except SQLAlchemyError as exc:
                log_event(logger, f"could not release model lock: {exc}",
                          level=logging.WARNING, adaptation_job_id=claim_.job_id)

        def _on_abandoned() -> None:
            # Thread mode: the stopped attempt's thread finished; now the lock may go.
            if abandoned.is_set():
                _release()

        timeout_s = self._timeout_s(claim_)
        try:
            if timeout_s <= 0:
                raise JobTimeoutError("the job's deadline passed before it ran",
                                      timeout_s=0.0, job_id=claim_.job_id)
            result = jobs._run_once(
                DriftEvent.model_validate(claim_.event),
                self.settings,
                executor=executor,
                registry=self.registry,
                llm_client=self.llm_client,
                workdir=os.path.join(self.workdir, claim_.job_id),
                session_factory=self.session_factory,
                job_id=claim_.job_id,
                lease_token=claim_.lease_token,
                timeout_s=timeout_s,
                on_tick=_Supervisor(self.session_factory, self.settings, claim_, self),
                on_abandoned=_on_abandoned,
            )
        except Exception as exc:  # noqa: BLE001 - every attempt outcome is recorded below
            stopped = isinstance(exc, JobTimeoutError | JobCancelledError | JobDrainedError |
                                 JobLeaseLostError)
            # A stopped thread-mode attempt keeps running until it finishes: it keeps the model
            # lock until then (on_abandoned), and the fence refuses whatever it would record.
            keep_lock = executor.in_process and stopped
            if keep_lock:
                abandoned.set()
            self._record_failure(claim_, exc, release_model_lock=not keep_lock)
            return
        final = JobStatus.ROLLED_BACK if result.outcome == "ROLLED_BACK" else JobStatus.COMPLETED
        self._record(claim_, final, message=result.reason,
                     result=result.model_dump(mode="json"),
                     strategy=result.strategy.value if result.strategy else None)

    def _record(self, claim_: Claim, to_status: JobStatus, *, message: str,
                error: dict | None = None, result: dict | None = None,
                strategy: str | None = None, release_model_lock: bool = True) -> None:
        """A terminal outcome, fenced by the claim's lease token."""
        try:
            with session_scope(self.session_factory) as session:
                job = jobs.load_job(session, claim_.job_id, lock=True)
                jobs.check_fence(job, claim_.lease_token, to_status)
                jobs.apply_transition(session, self.settings, job, to_status, message=message,
                                      error=error, result=result, strategy=strategy)
                _end_attempt(session, job, claim_, release_model_lock=release_model_lock)
                session.flush()
                response = jobs._to_response(job, duplicate=False)
        except (JobLeaseLostError, InvalidTransitionError) as exc:
            log_event(logger, f"outcome not recorded: {exc.message}", level=logging.WARNING,
                      adaptation_job_id=claim_.job_id)
            return
        jobs.count_outcome(response)
        log_event(logger, message or f"job -> {to_status}", adaptation_job_id=claim_.job_id,
                  status=to_status)

    def _record_failure(self, claim_: Claim, exc: Exception, *,
                        release_model_lock: bool) -> None:
        if isinstance(exc, JobLeaseLostError):
            log_event(logger, f"attempt stopped: {exc.message}", level=logging.WARNING,
                      adaptation_job_id=claim_.job_id)
            return
        if isinstance(exc, JobTimeoutError):
            with session_scope(self.session_factory) as session:
                last_stage = jobs.load_job(session, claim_.job_id).status
            error = jobs.timeout_error(last_stage, exc)
            self._record(claim_, JobStatus.TIMED_OUT, message=str(exc), error=error,
                         release_model_lock=release_model_lock)
            return
        if isinstance(exc, JobCancelledError):
            metrics.JOB_CANCELLED.labels("running").inc()
            self._record(claim_, JobStatus.CANCELLED, message=exc.message, error=exc.to_dict(),
                         release_model_lock=release_model_lock)
            return
        if isinstance(exc, (JobDrainedError, JobWorkerLostError, *_RETRYABLE)):
            self._requeue_or_end(claim_, exc)
            return
        error = (
            exc.to_dict()
            if isinstance(exc, AdaptationError)
            else {"code": "INTERNAL_ERROR", "message": str(exc)}
        )
        self._record(claim_, JobStatus.FAILED, message=str(exc), error=error,
                     release_model_lock=release_model_lock)

    def _requeue_or_end(self, claim_: Claim, exc: AdaptationError) -> None:
        """A drained, lost or transiently failed attempt: requeue the job, or end it."""
        settings = self.settings
        ended = False
        try:
            with session_scope(self.session_factory) as session:
                job = jobs.load_job(session, claim_.job_id, lock=True)
                jobs.check_fence(job, claim_.lease_token, JobStatus.QUEUED)
                if isinstance(exc, JobDrainedError):
                    requeue(session, settings, job, reason="drained", charge=False,
                            message=f"requeued: {exc.message}")
                elif isinstance(exc, JobWorkerLostError):
                    ended = lose(session, settings, job, why="the job's worker process died",
                                 reason="lost")
                else:
                    tries = job.attempt - (job.lost_count or 0)
                    if tries <= settings.job_max_retries:
                        backoff = settings.job_retry_backoff_s * (2 ** (tries - 1))
                        requeue(session, settings, job, reason="retry", delay_s=backoff,
                                message=f"retry {tries}/{settings.job_max_retries} after "
                                        f"{exc.code}: {exc.message}")
                    else:
                        jobs.apply_transition(session, settings, job, JobStatus.FAILED,
                                              message=str(exc), error=exc.to_dict())
                        ended = True
                _end_attempt(session, job, claim_, release_model_lock=ended)
                session.flush()
                response = jobs._to_response(job, duplicate=False)
        except (JobLeaseLostError, InvalidTransitionError) as lost:
            log_event(logger, f"outcome not recorded: {lost.message}", level=logging.WARNING,
                      adaptation_job_id=claim_.job_id)
            return
        if ended:
            jobs.count_outcome(response)
        elif self.queue is not None and not self.queue.runs_inline:
            # A retry becomes available after its backoff: the reaper publishes it then.
            log_event(logger, "job requeued", adaptation_job_id=claim_.job_id)


def _unpublish(session_factory, job_id: str) -> None:
    """A delivered message could not be acted on: have the reaper publish the job again."""
    with session_scope(session_factory) as session:
        session.execute(
            update(AdaptationJob)
            .where(AdaptationJob.job_id == job_id,
                   AdaptationJob.status == JobStatus.QUEUED.value,
                   AdaptationJob.lease_token.is_(None))
            .values(published_at=None)
        )


def reap(session_factory, settings: Settings, queue: JobQueuePort) -> dict[str, int]:
    """One reaper pass (module docstring). Safe to run from every worker at once: each change is
    a conditional update only one of them wins. Returns counts per action."""
    now = _now()
    counts = {"lease_expired": 0, "deadline": 0, "published": 0, "slots_freed": 0}

    with session_scope(session_factory) as session:
        expired = session.execute(
            select(AdaptationJob.job_id, AdaptationJob.lease_token)
            .where(AdaptationJob.lease_token.is_not(None),
                   AdaptationJob.lease_expires_at < now,
                   AdaptationJob.status.in_(_ACTIVE))
        ).all()
    for job_id, token in expired:
        with session_scope(session_factory) as session:
            taken = _cursor(session.execute(
                update(AdaptationJob)
                .where(AdaptationJob.job_id == job_id, AdaptationJob.lease_token == token)
                .values(lease_token=None, lease_expires_at=None)
            )).rowcount == 1
            if not taken:
                continue
            job = jobs.load_job(session, job_id)
            ended = lose(session, settings, job, reason="lease_expired",
                         why=f"the lease of worker {job.lease_owner} expired")
            session.execute(delete(JobSlot).where(JobSlot.job_id == job_id))
            if ended:
                release_lock(session, job.model_id, job_id)
        counts["lease_expired"] += 1
        log_event(logger, "expired lease reaped", level=logging.WARNING, adaptation_job_id=job_id)

    with session_scope(session_factory) as session:
        overdue = list(session.scalars(
            select(AdaptationJob.job_id)
            .where(AdaptationJob.status == JobStatus.QUEUED.value,
                   AdaptationJob.lease_token.is_(None),
                   AdaptationJob.deadline_at.is_not(None),
                   AdaptationJob.deadline_at < now)
        ))
    for job_id in overdue:
        with session_scope(session_factory) as session:
            job = jobs.load_job(session, job_id, lock=True)
            if job.status != JobStatus.QUEUED.value or job.lease_token is not None:
                continue
            exc = JobTimeoutError("the job's deadline passed while it was queued",
                                  timeout_s=settings.job_deadline_s, job_id=job_id)
            jobs.apply_transition(session, settings, job, JobStatus.TIMED_OUT,
                                  message=exc.message,
                                  error=jobs.timeout_error(JobStatus.QUEUED.value, exc))
            release_lock(session, job.model_id, job_id)
        counts["deadline"] += 1

    republish_before = now - timedelta(seconds=settings.job_republish_after_s)
    if not queue.runs_inline:
        with session_scope(session_factory) as session:
            due = list(session.scalars(
                select(AdaptationJob.job_id)
                .where(AdaptationJob.status == JobStatus.QUEUED.value,
                       AdaptationJob.lease_token.is_(None),
                       AdaptationJob.quarantined.is_(False),
                       or_(AdaptationJob.available_at.is_(None),
                           AdaptationJob.available_at <= now),
                       or_(AdaptationJob.published_at.is_(None),
                           AdaptationJob.published_at < republish_before))
                .order_by(AdaptationJob.priority.desc(), AdaptationJob.created_at)
            ))
        for job_id in due:
            try:
                if not jobs.publish(session_factory, queue, job_id):
                    break
            except JobQueueUnavailableError:
                break
            counts["published"] += 1

    with session_scope(session_factory) as session:
        counts["slots_freed"] = _cursor(
            session.execute(delete(JobSlot).where(JobSlot.expires_at < now))
        ).rowcount
        _set_queue_gauges(session, settings, now)
    return counts


def _set_queue_gauges(session: Session, settings: Settings, now: datetime) -> None:
    rows = session.execute(
        select(AdaptationJob.worker_class, func.count(), func.min(AdaptationJob.created_at))
        .where(and_(AdaptationJob.status == JobStatus.QUEUED.value,
                    AdaptationJob.quarantined.is_(False)))
        .group_by(AdaptationJob.worker_class)
    ).all()
    seen = set()
    for worker_class, depth, oldest in rows:
        seen.add(worker_class)
        metrics.JOB_QUEUE_DEPTH.labels(worker_class).set(depth)
        oldest_at = jobs.aware(oldest)
        age = (now - oldest_at).total_seconds() if oldest_at is not None else 0.0
        metrics.JOB_QUEUE_OLDEST_AGE.labels(worker_class).set(max(age, 0.0))
    for worker_class in set(settings.job_worker_classes) - seen:
        metrics.JOB_QUEUE_DEPTH.labels(worker_class).set(0)
        metrics.JOB_QUEUE_OLDEST_AGE.labels(worker_class).set(0)


def worker_from_settings(settings: Settings | None = None, *,
                         classes: Iterable[str] | None = None) -> Worker:
    """A worker built from the configuration alone (the CLI and the broker entry points)."""
    settings = settings or get_settings()
    factory = make_session_factory(create_db_engine(settings.database_url))
    return Worker(factory, settings, registry=build_registry(settings),
                  llm_client=build_llm(settings), workdir=settings.artifact_workdir,
                  queue=build_job_queue(settings), classes=classes)


def run_job_by_id(job_id: str) -> bool:
    """What a broker message runs (celery task, RQ job, Kubernetes Job): one attempt of
    ``job_id`` on a worker built from the environment's configuration."""
    return worker_from_settings().run_job(job_id)


def install_drain_handlers(worker: Worker) -> None:
    """Drain ``worker`` on SIGTERM and SIGINT, and on Ctrl+Break on Windows (SIGBREAK). Must be
    called from the main thread."""
    def _handler(signum: int, _frame: object) -> None:
        log_event(logger, f"signal {signum} received: draining")
        worker.drain()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _handler)
