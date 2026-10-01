"""Progressive delivery: how a candidate that passed the validation gate reaches traffic.

DELIVERY_STRATEGY picks the strategy, DELIVERY_POLICY its thresholds. ``blue_green`` is the
inline promotion the pipeline has always done (no rollout row); every other strategy starts a
Rollout here and the controller moves it on each tick (the worker loop every ROLLOUT_TICK_S, or
``oran-adapt rollout tick``):

    shadow   SHADOW: the candidate scores mirrored traffic (serving reads
             ``model://<name>@<CANDIDATE_ALIAS>``) and serves none. Healthy with enough samples:
             ``shadow_then`` (promote, canary, or manual approval). No verdict by
             ``shadow_max_s``: EXPIRED.
    canary   CANARY: ``canary_steps`` percentages, each held ``canary_step_hold_s`` and healthy
             with enough samples before the next; the step at 100 is the promotion.
    ab       AB: ``ab_percent`` for ``ab_duration_s``, then a Welch t-test on ``ab_metric``:
             better promotes, worse rolls back, inconclusive does ``ab_inconclusive``.
    manual   AWAITING_APPROVAL: an approver has ``approval_ttl_s`` (then EXPIRED); approving
             does ``approval_then`` (promote, or continue as a canary).

A health breach in any state rolls back: the split is removed and read back (the serving system
reports all traffic on the stable version) before the rollout records ROLLED_BACK. Promotion is
registry.promotion.promote_version, conditional on LIVE still being the stable version, with an
idempotency key per rollout, then the split is cleared. A failed rollback or clear leaves the
rollout where it was; the next tick decides again from the same evidence.

Each tick holds the model's lock (orchestrator.locks) under the holder ``rollout:<id>``, so a
tick never interleaves with an adaptation job or with another replica's tick of the same model.
The policy the rollout started under is stored on it: a later config change does not alter a
rollout in progress.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oran_adapt.core import metrics, tracing
from oran_adapt.core.audit import record_audit
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import AuditAction, PromotionKind
from oran_adapt.core.errors import (
    AdaptationError,
    ConflictError,
    DeploymentError,
    ModelNotFoundError,
    PromotionError,
    RolloutMetricsUnavailableError,
    RolloutNotFoundError,
    RolloutStateError,
)
from oran_adapt.core.logging import log_event
from oran_adapt.core.policies import DeliveryPolicy, policy_hash
from oran_adapt.db.models import AdaptationJob, ModelMetadata, Rollout, RolloutObservation
from oran_adapt.delivery.health import BREACH, HEALTHY, ab_test, evaluate_health
from oran_adapt.notifications.events import record_event
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.ports import (
    ArmStats,
    ArmWindow,
    DeploymentTarget,
    ModelRegistryPort,
    RolloutMetricsPort,
)
from oran_adapt.registry.deployment import Deployer
from oran_adapt.registry.promotion import promote_version

logger = logging.getLogger(__name__)

SHADOW = "SHADOW"
CANARY = "CANARY"
AB = "AB"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
PROMOTED = "PROMOTED"
ROLLED_BACK = "ROLLED_BACK"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
ACTIVE_STATES = (SHADOW, CANARY, AB, AWAITING_APPROVAL)
FINAL_STATES = (PROMOTED, ROLLED_BACK, EXPIRED, REJECTED)
STRATEGIES = ("shadow", "canary", "ab", "manual")
ARMS = ("stable", "candidate")
COMPONENT = "delivery"


def _now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime (SQLite returns them naive)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass
class Delivery:
    """What the controller acts through: the registry, the serving system (with read-back)
    and the rollout metrics source."""

    settings: Settings
    registry: ModelRegistryPort
    deployer: Deployer
    source: RolloutMetricsPort
    workdir: str

    @classmethod
    def from_settings(cls, settings: Settings, registry: ModelRegistryPort, *,
                      deployer: Deployer | None = None,
                      source: RolloutMetricsPort | None = None,
                      workdir: str | None = None) -> Delivery:
        from oran_adapt.bootstrap import build_deployer, build_rollout_metrics

        return cls(
            settings=settings,
            registry=registry,
            deployer=deployer or build_deployer(settings, registry),
            source=source or build_rollout_metrics(settings),
            workdir=workdir or os.path.join(settings.artifact_workdir, "rollouts"),
        )


def to_dict(rollout: Rollout) -> dict[str, Any]:
    """The rollout as the API and the job result show it."""
    def iso(value: datetime | None) -> str | None:
        value = aware(value)
        return value.isoformat() if value else None

    return {
        "rollout_id": rollout.rollout_id,
        "model_id": rollout.model_id,
        "job_id": rollout.job_id,
        "strategy": rollout.strategy,
        "state": rollout.state,
        "active": rollout.state in ACTIVE_STATES,
        "stable_version": rollout.stable_version,
        "candidate_version": rollout.candidate_version,
        "step": rollout.step,
        "percent": rollout.percent,
        "policy": rollout.policy,
        "policy_hash": rollout.policy_hash,
        "gate_decision_id": rollout.gate_decision_id,
        "step_started_at": iso(rollout.step_started_at),
        "deadline_at": iso(rollout.deadline_at),
        "decided_by": rollout.decided_by,
        "reason": rollout.reason,
        "history": list(rollout.history or []),
        "created_at": iso(rollout.created_at),
        "updated_at": iso(rollout.updated_at),
        "finished_at": iso(rollout.finished_at),
    }


# -- lookups ---------------------------------------------------------------------------------


def active_rollout(session: Session, model_id: str) -> Rollout | None:
    return session.execute(
        select(Rollout)
        .where(Rollout.model_id == model_id, Rollout.state.in_(ACTIVE_STATES))
        .order_by(Rollout.id.desc())
    ).scalars().first()


def get_rollout(session: Session, rollout_id: str) -> Rollout:
    row = session.execute(
        select(Rollout).where(Rollout.rollout_id == rollout_id)
    ).scalar_one_or_none()
    if row is None:
        raise RolloutNotFoundError(f"rollout '{rollout_id}' not found", rollout_id=rollout_id)
    return row


def list_rollouts(session: Session, *, model_id: str | None = None, state: str | None = None,
                  active: bool | None = None, limit: int = 50, offset: int = 0) -> list[Rollout]:
    query = select(Rollout)
    if model_id is not None:
        query = query.where(Rollout.model_id == model_id)
    if state is not None:
        query = query.where(Rollout.state == state)
    if active is True:
        query = query.where(Rollout.state.in_(ACTIVE_STATES))
    elif active is False:
        query = query.where(Rollout.state.in_(FINAL_STATES))
    return list(session.execute(
        query.order_by(Rollout.id.desc()).limit(limit).offset(offset)
    ).scalars())


def _name(session: Session, model_id: str) -> str:
    meta = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    ).scalar_one_or_none()
    if meta is None:
        raise ModelNotFoundError(f"model '{model_id}' not found", model_id=model_id)
    return meta.mlflow_model_name


def _target(registry: ModelRegistryPort, name: str, version: str) -> DeploymentTarget:
    try:
        source = registry.get_version(name, version).source
    except ModelNotFoundError:
        source = None
    return DeploymentTarget(model=name, version=version, source=source)


def _policy(rollout: Rollout) -> DeliveryPolicy:
    return DeliveryPolicy.model_validate(rollout.policy)


# -- transitions -----------------------------------------------------------------------------


def _record(session: Session, delivery: Delivery, rollout: Rollout, *, to: str, reason: str,
            now: datetime, actor: str = "system", detail: dict | None = None,
            percent: int | None = None, step: int | None = None,
            deadline: datetime | None = None) -> None:
    """Move ``rollout`` to ``to`` and record it: history, audit, metrics, notification."""
    before = rollout.state
    if percent is not None:
        rollout.percent = percent
    if step is not None:
        rollout.step = step
    rollout.state = to
    rollout.reason = reason
    rollout.step_started_at = now
    rollout.deadline_at = deadline
    rollout.updated_at = now
    entry = {"at": now.isoformat(), "from": before, "to": to, "step": rollout.step,
             "percent": rollout.percent, "actor": actor, "reason": reason}
    if detail:
        entry["detail"] = detail
    rollout.history = [*list(rollout.history or []), entry]
    final = to in FINAL_STATES
    if final:
        rollout.finished_at = now
        metrics.ROLLOUTS_FINISHED.labels(strategy=rollout.strategy, state=to).inc()
    else:
        metrics.ROLLOUT_STEPS.labels(strategy=rollout.strategy, state=to).inc()
    record_audit(
        session,
        AuditAction.ROLLOUT_FINISHED if final else AuditAction.ROLLOUT_ADVANCED,
        component=COMPONENT, actor=actor, job_id=rollout.job_id, model_id=rollout.model_id,
        model_version=rollout.candidate_version, decision=to, reason=reason,
        metadata={"rollout_id": rollout.rollout_id, "from": before, "percent": rollout.percent,
                  "step": rollout.step, "detail": detail or {}},
    )
    record_event(
        session, delivery.settings, event_type=f"rollout.{to.lower()}",
        subject=rollout.rollout_id, model_id=rollout.model_id,
        data={"rollout_id": rollout.rollout_id, "model_id": rollout.model_id,
              "strategy": rollout.strategy, "from_state": before, "to_state": to,
              "stable_version": rollout.stable_version,
              "candidate_version": rollout.candidate_version, "percent": rollout.percent,
              "reason": reason},
    )
    log_event(logger, "rollout transition", rollout_id=rollout.rollout_id,
              model_id=rollout.model_id, from_state=before, to_state=to,
              percent=rollout.percent, reason=reason)


def _note(rollout: Rollout, now: datetime, what: str, **detail: Any) -> None:
    """A history entry that is not a transition (a failed attempt, a hold)."""
    rollout.history = [*list(rollout.history or []),
                       {"at": now.isoformat(), "note": what, **detail}]
    rollout.updated_at = now


def _split(session: Session, delivery: Delivery, rollout: Rollout, percent: int) -> None:
    """Route ``percent`` of traffic to the candidate and read it back (0: remove the split)."""
    name = _name(session, rollout.model_id)
    assert rollout.stable_version is not None
    stable = _target(delivery.registry, name, rollout.stable_version)
    candidate = (_target(delivery.registry, name, rollout.candidate_version)
                 if percent else None)
    delivery.deployer.split(name, stable=stable, candidate=candidate, percent=percent)


def _undo_traffic(session: Session, delivery: Delivery, rollout: Rollout) -> None:
    """Remove any split, reading back all traffic on the stable version. The system is asked
    too, so a split left by a half-applied change is removed as well."""
    if not delivery.deployer.splits_traffic or rollout.stable_version is None:
        return
    if rollout.percent > 0 or delivery.deployer.traffic(
        _name(session, rollout.model_id)
    ).candidate is not None:
        _split(session, delivery, rollout, 0)


def _end_without_promotion(session: Session, delivery: Delivery, rollout: Rollout, *,
                           to: str, reason: str, now: datetime, actor: str = "system",
                           detail: dict | None = None) -> None:
    """ROLLED_BACK / EXPIRED / REJECTED: undo the split first; a failed undo raises
    DeploymentError and leaves the rollout as it was."""
    _undo_traffic(session, delivery, rollout)
    if to == ROLLED_BACK:
        metrics.ROLLBACK.labels("rollout").inc()
    _record(session, delivery, rollout, to=to, reason=reason, now=now, actor=actor,
            detail=detail, percent=0)
    session.commit()


def _promote(session: Session, delivery: Delivery, rollout: Rollout, *, reason: str,
             now: datetime, actor: str = "system", detail: dict | None = None) -> None:
    """Move LIVE to the candidate (conditional on LIVE still being stable), then clear the
    split. A failed promotion restores LIVE (promote_version) and rolls the rollout back."""
    settings = delivery.settings
    session.commit()  # promote_version commits or rolls back the session itself
    try:
        promotion = promote_version(
            session,
            delivery.registry,
            deployer=delivery.deployer,
            model_id=rollout.model_id,
            version=rollout.candidate_version,
            kind=PromotionKind.PROMOTE_CANDIDATE,
            live_alias=settings.live_alias,
            workdir=os.path.join(delivery.workdir, rollout.rollout_id),
            reason=reason,
            actor=actor,
            job_id=rollout.job_id,
            expected_live=rollout.stable_version,
            idempotency_key=f"rollout:{rollout.rollout_id}:promote",
        )
    except (PromotionError, ConflictError) as exc:
        session.rollback()
        metrics.DELIVERY_FAILURES.labels(strategy=rollout.strategy, reason="promotion").inc()
        why = f"promotion failed, candidate not live: {exc.message}"
        _end_without_promotion(session, delivery, rollout, to=ROLLED_BACK, reason=why, now=now,
                               actor=actor, detail={**(detail or {}), "error": exc.to_dict()})
        return
    # LIVE is the candidate now: it is also the stable side of the (removed) split.
    if delivery.deployer.splits_traffic and rollout.percent > 0:
        name = _name(session, rollout.model_id)
        live = _target(delivery.registry, name, rollout.candidate_version)
        try:
            delivery.deployer.split(name, stable=live, candidate=None, percent=0)
        except DeploymentError as exc:
            # LIVE and the serving system already agree on the candidate; only the leftover
            # split could not be read back as removed. Recorded; operators see it in history.
            _note(rollout, now, "split not cleared after promotion", error=exc.to_dict())
    _record(session, delivery, rollout, to=PROMOTED, reason=reason, now=now, actor=actor,
            detail={**(detail or {}), "promotion": promotion.to_dict()}, percent=100)
    session.commit()


def _enter_canary(session: Session, delivery: Delivery, rollout: Rollout, *, reason: str,
                  now: datetime, actor: str = "system") -> None:
    policy = _policy(rollout)
    first = policy.canary_steps[0]
    if first >= 100:
        _promote(session, delivery, rollout, reason=reason, now=now, actor=actor)
        return
    _split(session, delivery, rollout, first)
    _record(session, delivery, rollout, to=CANARY, reason=reason, now=now, actor=actor,
            percent=first, step=0)
    session.commit()


# -- start -----------------------------------------------------------------------------------


def start_rollout(session: Session, delivery: Delivery, *, model_id: str, strategy: str,
                  stable_version: str, candidate_version: str, job_id: str | None = None,
                  gate_decision_id: int | None = None, policy: DeliveryPolicy | None = None,
                  now: datetime | None = None) -> Rollout:
    """Start delivering ``candidate_version``. The first traffic change (canary, A/B) is made
    and read back before this returns; if it fails the rollout is recorded ROLLED_BACK."""
    if strategy not in STRATEGIES:
        raise RolloutStateError(f"'{strategy}' is not a progressive delivery strategy",
                                strategy=strategy, strategies=list(STRATEGIES))
    existing = active_rollout(session, model_id)
    if existing is not None:
        raise RolloutStateError(
            f"model '{model_id}' already has rollout {existing.rollout_id} in progress",
            model_id=model_id, rollout_id=existing.rollout_id, state=existing.state,
        )
    now = now or _now()
    policy = policy or delivery.settings.delivery_policy
    initial = {"shadow": SHADOW, "canary": CANARY, "ab": AB, "manual": AWAITING_APPROVAL}[strategy]
    rollout = Rollout(
        rollout_id=uuid.uuid4().hex,
        model_id=model_id,
        job_id=job_id,
        strategy=strategy,
        state=initial,
        stable_version=stable_version,
        candidate_version=candidate_version,
        step=0,
        percent=0,
        policy=policy.model_dump(mode="json"),
        policy_hash=policy_hash(policy),
        gate_decision_id=gate_decision_id,
        step_started_at=now,
        reason="",
        history=[],
        created_at=now,
        updated_at=now,
    )
    session.add(rollout)
    session.flush()
    metrics.ROLLOUTS_STARTED.labels(strategy=strategy).inc()
    record_audit(
        session, AuditAction.ROLLOUT_STARTED, component=COMPONENT, job_id=job_id,
        model_id=model_id, model_version=candidate_version, decision=strategy,
        reason=f"delivering version {candidate_version} ({strategy}); stable {stable_version}",
        metadata={"rollout_id": rollout.rollout_id, "policy_version": policy.version,
                  "policy_hash": rollout.policy_hash, "gate_decision_id": gate_decision_id},
    )
    percent = {"canary": policy.canary_steps[0], "ab": policy.ab_percent}.get(strategy, 0)
    deadline = {
        "shadow": now + timedelta(seconds=policy.shadow_max_s),
        "ab": now + timedelta(seconds=policy.ab_duration_s),
        "manual": now + timedelta(seconds=policy.approval_ttl_s),
    }.get(strategy)
    reason = {
        "shadow": "candidate scores mirrored traffic",
        "canary": f"canary step 1 of {len(policy.canary_steps)} at {percent}%",
        "ab": f"A/B split at {percent}% for {policy.ab_duration_s:g}s",
        "manual": f"awaiting approval for {policy.approval_ttl_s:g}s",
    }[strategy]
    if percent >= 100:  # canary_steps == [100]: nothing to hold, promote at once
        _record(session, delivery, rollout, to=initial, reason=reason, now=now)
        _promote(session, delivery, rollout, reason="single canary step at 100%", now=now)
        return rollout
    if percent:
        try:
            _split(session, delivery, rollout, percent)
        except DeploymentError as exc:
            # The split never read back: make sure no candidate traffic is left, then end.
            metrics.DELIVERY_FAILURES.labels(strategy=strategy, reason="first_split").inc()
            try:
                _end_without_promotion(
                    session, delivery, rollout, to=ROLLED_BACK,
                    reason=f"the first traffic split failed: {exc.message}", now=now,
                    detail={"error": exc.to_dict()},
                )
            except DeploymentError as undo:
                _note(rollout, now, "split undo failed", error=undo.to_dict())
                session.commit()
            return rollout
    _record(session, delivery, rollout, to=initial, reason=reason, now=now, percent=percent,
            deadline=deadline)
    session.commit()
    return rollout


# -- tick ------------------------------------------------------------------------------------


def _stats(delivery: Delivery, session: Session, rollout: Rollout, name: str,
           now: datetime) -> tuple[ArmStats, ArmStats]:
    start = aware(rollout.step_started_at) or now
    assert rollout.stable_version is not None

    def arm(arm_name: str, version: str) -> ArmStats:
        return delivery.source.observe(session, ArmWindow(
            rollout_id=rollout.rollout_id, model=name, arm=arm_name, version=version,
            start=start, end=now,
        ))

    return arm("stable", rollout.stable_version), arm("candidate", rollout.candidate_version)


def _lock(session: Session, settings: Settings, model_id: str, holder: str) -> bool:
    if take_over_expired_lock(session, model_id, holder, settings.model_lock_ttl_s):
        session.commit()
        return True
    add_lock(session, model_id, holder, settings.model_lock_ttl_s)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    session.commit()
    return True


def _advance(session: Session, delivery: Delivery, rollout: Rollout, now: datetime) -> str:
    """One decision for an active rollout; returns what happened (for the tick summary)."""
    policy = _policy(rollout)
    deadline = aware(rollout.deadline_at)
    if rollout.state == AWAITING_APPROVAL:
        if deadline is not None and now >= deadline:
            _end_without_promotion(session, delivery, rollout, to=EXPIRED, now=now,
                                   reason="no approval within approval_ttl_s")
            return EXPIRED
        return "waiting"

    name = _name(session, rollout.model_id)
    stable, candidate = _stats(delivery, session, rollout, name, now)
    health = evaluate_health(policy, stable, candidate)
    evidence = {"health": health.as_dict(), "stable_requests": stable.count,
                "candidate_requests": candidate.count}
    if health.status == BREACH:
        _end_without_promotion(session, delivery, rollout, to=ROLLED_BACK, now=now,
                               reason="health breach: " + "; ".join(health.reasons),
                               detail=evidence)
        return ROLLED_BACK

    if rollout.state == SHADOW:
        if health.status == HEALTHY:
            then = policy.shadow_then
            why = f"shadow healthy on {candidate.count} requests; then {then}"
            if then == "promote":
                _promote(session, delivery, rollout, reason=why, now=now, detail=evidence)
                return PROMOTED
            if then == "canary":
                _enter_canary(session, delivery, rollout, reason=why, now=now)
                return CANARY
            _record(session, delivery, rollout, to=AWAITING_APPROVAL, reason=why, now=now,
                    detail=evidence,
                    deadline=now + timedelta(seconds=policy.approval_ttl_s))
            session.commit()
            return AWAITING_APPROVAL
        if deadline is not None and now >= deadline:
            _end_without_promotion(session, delivery, rollout, to=EXPIRED, now=now,
                                   reason="no health verdict within shadow_max_s: "
                                   + "; ".join(health.reasons), detail=evidence)
            return EXPIRED
        return "waiting"

    if rollout.state == CANARY:
        held = (now - (aware(rollout.step_started_at) or now)).total_seconds()
        if health.status != HEALTHY or held < policy.canary_step_hold_s:
            return "waiting"
        nxt = rollout.step + 1
        percent = policy.canary_steps[nxt]
        why = (f"canary step {rollout.step + 1} at {rollout.percent}% healthy for "
               f"{held:.0f}s on {candidate.count} requests")
        if percent >= 100:
            _promote(session, delivery, rollout, reason=why, now=now, detail=evidence)
            return PROMOTED
        _split(session, delivery, rollout, percent)
        _record(session, delivery, rollout, to=CANARY, reason=why + f"; now {percent}%",
                now=now, detail=evidence, percent=percent, step=nxt)
        session.commit()
        return CANARY

    # AB
    if deadline is not None and now < deadline:
        return "waiting"
    result = ab_test(policy, stable, candidate)
    evidence["ab"] = result.as_dict()
    outcome = {"better": "promote", "worse": "rollback"}.get(result.verdict,
                                                             policy.ab_inconclusive)
    if outcome == "promote":
        _promote(session, delivery, rollout, reason=result.reason, now=now, detail=evidence)
        return PROMOTED
    if outcome == "manual":
        _record(session, delivery, rollout, to=AWAITING_APPROVAL, reason=result.reason,
                now=now, detail=evidence,
                deadline=now + timedelta(seconds=policy.approval_ttl_s))
        session.commit()
        return AWAITING_APPROVAL
    _end_without_promotion(session, delivery, rollout, to=ROLLED_BACK, reason=result.reason,
                           now=now, detail=evidence)
    return ROLLED_BACK


def _job_trace(session: Session, rollout: Rollout) -> tuple[str | None, str]:
    """The trace a rollout's ticks join: that of the job that started it (core.tracing)."""
    if rollout.job_id is None:
        return None, f"rollout:{rollout.rollout_id}"
    row = session.execute(
        select(AdaptationJob.trace_context, AdaptationJob.idempotency_key, AdaptationJob.event)
        .where(AdaptationJob.job_id == rollout.job_id)
    ).one_or_none()
    if row is None:
        return None, f"rollout:{rollout.rollout_id}"
    return row.trace_context, (row.event or {}).get("event_id") or row.idempotency_key


def tick(session: Session, delivery: Delivery, rollout_id: str,
         now: datetime | None = None) -> str:
    """Advance one rollout under its model's lock. Returns ``busy`` when the model is locked
    (a job or another tick holds it), ``unavailable`` when the metrics source or the serving
    system failed (nothing changed; the next tick tries again), ``ended`` for a rollout that
    already ended, else what the controller did (``waiting`` or the new state)."""
    now = now or _now()
    rollout = get_rollout(session, rollout_id)
    if rollout.state not in ACTIVE_STATES:
        return "ended"
    holder = f"rollout:{rollout_id}"
    model_id = rollout.model_id
    if not _lock(session, delivery.settings, model_id, holder):
        return "busy"
    try:
        rollout = get_rollout(session, rollout_id)
        if rollout.state not in ACTIVE_STATES:
            return "ended"
        traceparent, key = _job_trace(session, rollout)
        try:
            with tracing.continued("rollout.tick", traceparent, key=key,
                                   rollout_id=rollout_id, strategy=rollout.strategy,
                                   state=rollout.state) as span:
                outcome = _advance(session, delivery, rollout, now)
                span.set_attribute("oran.outcome", outcome)
                return outcome
        except (RolloutMetricsUnavailableError, DeploymentError) as exc:
            reason = "metrics" if isinstance(exc, RolloutMetricsUnavailableError) else "serving"
            metrics.DELIVERY_FAILURES.labels(strategy=rollout.strategy, reason=reason).inc()
            session.rollback()
            rollout = get_rollout(session, rollout_id)
            _note(rollout, now, "tick could not complete", error=exc.to_dict())
            session.commit()
            log_event(logger, "rollout tick could not complete", level=logging.WARNING,
                      rollout_id=rollout_id, cause=exc.message)
            return "unavailable"
    finally:
        session.rollback()
        release_lock(session, model_id, holder)
        session.commit()


def tick_all(session: Session, delivery: Delivery, now: datetime | None = None) -> dict[str, str]:
    """Tick every active rollout; returns rollout_id -> what happened. Also sets the
    ``rollouts_active`` gauge."""
    ids = [r.rollout_id for r in list_rollouts(session, active=True, limit=10_000)]
    out: dict[str, str] = {}
    for rollout_id in ids:
        try:
            out[rollout_id] = tick(session, delivery, rollout_id, now)
        except AdaptationError as exc:
            session.rollback()
            out[rollout_id] = f"error: {exc.code}"
            log_event(logger, "rollout tick failed", level=logging.ERROR,
                      rollout_id=rollout_id, cause=exc.message)
    set_active_gauge(session)
    return out


def set_active_gauge(session: Session) -> None:
    counts = {state: 0 for state in ACTIVE_STATES}
    for rollout in list_rollouts(session, active=True, limit=10_000):
        counts[rollout.state] += 1
    for state, count in counts.items():
        metrics.ROLLOUTS_ACTIVE.labels(state=state).set(count)


# -- operator actions ------------------------------------------------------------------------


def _locked_action(session: Session, delivery: Delivery, rollout_id: str) -> tuple[Rollout, str]:
    rollout = get_rollout(session, rollout_id)
    if rollout.state not in ACTIVE_STATES:
        raise RolloutStateError(f"rollout '{rollout_id}' already ended ({rollout.state})",
                                rollout_id=rollout_id, state=rollout.state)
    holder = f"rollout:{rollout_id}"
    if not _lock(session, delivery.settings, rollout.model_id, holder):
        raise RolloutStateError(
            f"model '{rollout.model_id}' is busy (a job or a rollout tick holds its lock); "
            "try again", rollout_id=rollout_id, model_id=rollout.model_id,
        )
    return get_rollout(session, rollout_id), holder


def approve(session: Session, delivery: Delivery, rollout_id: str, *, actor: str,
            reason: str = "", now: datetime | None = None) -> Rollout:
    """Approve a rollout AWAITING_APPROVAL: ``approval_then`` runs (promote, or a canary).
    An approval after ``approval_ttl_s`` expires the rollout instead and is refused."""
    now = now or _now()
    rollout, holder = _locked_action(session, delivery, rollout_id)
    try:
        if rollout.state != AWAITING_APPROVAL:
            raise RolloutStateError(
                f"rollout '{rollout_id}' is {rollout.state}, not AWAITING_APPROVAL",
                rollout_id=rollout_id, state=rollout.state,
            )
        deadline = aware(rollout.deadline_at)
        if deadline is not None and now >= deadline:
            _end_without_promotion(session, delivery, rollout, to=EXPIRED, now=now,
                                   reason="approval came after approval_ttl_s")
            raise RolloutStateError(f"rollout '{rollout_id}' expired before it was approved",
                                    rollout_id=rollout_id, state=EXPIRED)
        rollout.decided_by = actor
        why = f"approved by {actor}" + (f": {reason}" if reason else "")
        if _policy(rollout).approval_then == "canary":
            _enter_canary(session, delivery, rollout, reason=why, now=now, actor=actor)
        else:
            _promote(session, delivery, rollout, reason=why, now=now, actor=actor)
        return get_rollout(session, rollout_id)
    finally:
        session.rollback()
        release_lock(session, rollout.model_id, holder)
        session.commit()


def reject(session: Session, delivery: Delivery, rollout_id: str, *, actor: str,
           reason: str = "", now: datetime | None = None) -> Rollout:
    """Stop an active rollout: any split is removed and read back, the candidate never goes
    live, the rollout records REJECTED."""
    now = now or _now()
    rollout, holder = _locked_action(session, delivery, rollout_id)
    try:
        rollout.decided_by = actor
        _end_without_promotion(session, delivery, rollout, to=REJECTED, now=now, actor=actor,
                               reason=f"rejected by {actor}" + (f": {reason}" if reason else ""))
        return get_rollout(session, rollout_id)
    finally:
        session.rollback()
        release_lock(session, rollout.model_id, holder)
        session.commit()


def observe(session: Session, rollout_id: str, *, arm: str, requests: int,
            metrics_: dict[str, float], observed_at: datetime | None = None) -> RolloutObservation:
    """Store one observation of an arm of an active rollout (the ``api`` metrics source)."""
    rollout = get_rollout(session, rollout_id)
    if rollout.state not in ACTIVE_STATES:
        raise RolloutStateError(f"rollout '{rollout_id}' already ended ({rollout.state})",
                                rollout_id=rollout_id, state=rollout.state)
    if arm not in ARMS:
        raise RolloutStateError(f"arm must be one of {', '.join(ARMS)}", arm=arm)
    row = RolloutObservation(rollout_id=rollout_id, arm=arm, requests=requests,
                             metrics=dict(metrics_), observed_at=observed_at or _now())
    session.add(row)
    session.commit()
    return row
