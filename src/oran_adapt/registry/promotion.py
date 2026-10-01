"""Moving a model's live alias: promoting a validated candidate, reusing an older version, or
rolling back. This is the only module that changes LIVE.

Every move is recorded as a ModelPromotion row whose ``from_version`` is what LIVE pointed at
before, so any move can be undone. A move is all-or-nothing across the database, the registry
and the serving system: the row is flushed first, then the alias is set and read back, then the
version is rolled out to the serving system (DEPLOYMENT_BACKEND) and read back from it
(``registry.deployment.Deployer``), then the version status tags are updated and the
transaction commits. If any step fails, the serving system and the alias are put back to the
previous version (or cleared, if there was none) and the database transaction is rolled back,
so LIVE never ends up pointing somewhere the history does not explain, and what serves traffic
never differs from LIVE after a failed move.

Integrity: before a version goes live its downloaded artifact is hashed and compared with the
``artifact.sha256`` tag written when it was registered. A mismatch refuses the move. A version
registered before checksums existed has no tag; its hash is recorded at first promotion (trust
on first use), which is logged so an operator can see it happened.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from oran_adapt.core import metrics
from oran_adapt.core.audit import record_audit
from oran_adapt.core.enums import AuditAction, ModelVersionStatus, PromotionKind
from oran_adapt.core.errors import (
    AdaptationError,
    ArtifactError,
    ConflictError,
    DeploymentError,
    ModelNotFoundError,
    PromotionError,
)
from oran_adapt.core.integrity import check_size, sha256_path, verify_checksum
from oran_adapt.core.logging import log_event
from oran_adapt.db.models import ModelMetadata, ModelPromotion
from oran_adapt.ports import DeploymentTarget, ModelHandlerPort, ModelRegistryPort
from oran_adapt.registry.deployment import Deployer

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class PromotionResult:
    model_id: str
    kind: str
    from_version: str | None
    to_version: str
    status: str  # APPLIED | NO_CHANGE
    promotion_id: int | None
    artifact_sha256: str | None
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _result(row: ModelPromotion) -> PromotionResult:
    return PromotionResult(
        model_id=row.model_id,
        kind=row.kind,
        from_version=row.from_version,
        to_version=row.to_version,
        status=row.status,
        promotion_id=row.id,
        artifact_sha256=row.artifact_sha256,
        reason=row.reason,
    )


def _model_meta(session: Session, model_id: str) -> ModelMetadata:
    meta = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    ).scalar_one_or_none()
    if meta is None:
        raise ModelNotFoundError(f"model '{model_id}' is not onboarded", model_id=model_id)
    return meta


def current_live(registry: ModelRegistryPort, name: str, alias: str) -> str | None:
    try:
        return registry.get_version_by_alias(name, alias)
    except ModelNotFoundError:
        return None


def verify_version_artifact(
    registry: ModelRegistryPort,
    name: str,
    version: str,
    workdir: str,
) -> tuple[str, str]:
    """Download ``version``, check its size and its checksum against the registration-time
    tag (both per ``registry.artifact_policy``), and return ``(local_path, sha256)``. Records the
    checksum when the version has none."""
    policy = registry.artifact_policy
    info = registry.get_version(name, version)
    local = registry.download_artifacts(name, version, os.path.join(workdir, f"verify-{version}"))
    check_size(local, policy.max_bytes, model=name, version=version)
    expected = (info.tags or {}).get(policy.checksum_tag)
    if expected:
        verify_checksum(
            local, expected, chunk_bytes=policy.hash_chunk_bytes, model=name, version=version
        )
        return local, expected
    digest = sha256_path(local, policy.hash_chunk_bytes)
    registry.set_version_tags(name, version, {policy.checksum_tag: digest})
    log_event(
        logger,
        "artifact had no recorded checksum; recorded it now (trust on first use)",
        model=name,
        version=version,
        sha256=digest,
    )
    return local, digest


def _serving_target(
    registry: ModelRegistryPort, deployer: Deployer, name: str
) -> DeploymentTarget | None:
    """What the serving system serves now, as a target a failed rollout can restore."""
    serving = deployer.serving(name)
    if serving is None:
        return None
    try:
        source = registry.get_version(name, serving).source
    except ModelNotFoundError:
        source = None  # served, but no longer (or never) in the registry: restore by version
    return DeploymentTarget(model=name, version=serving, source=source)


def _restore_alias(registry: ModelRegistryPort, name: str, alias: str, previous: str | None) -> None:
    try:
        if previous is None:
            registry.delete_alias(name, alias)
        else:
            registry.set_alias(name, alias, previous)
    except AdaptationError as exc:  # pragma: no cover - reported, nothing more we can do
        log_event(
            logger,
            "could not restore the live alias after a failed promotion",
            level=logging.ERROR,
            model=name,
            previous=previous,
            cause=exc.message,
        )


def promote_version(
    session: Session,
    registry: ModelRegistryPort,
    *,
    deployer: Deployer,
    model_id: str,
    version: str,
    kind: PromotionKind,
    live_alias: str,
    workdir: str,
    reason: str = "",
    actor: str = "system",
    job_id: str | None = None,
    expected_live: str | None = None,
    idempotency_key: str | None = None,
) -> PromotionResult:
    """Point ``live_alias`` at ``version``. Idempotent: promoting the version that is already
    live changes nothing (status NO_CHANGE), and a repeated ``idempotency_key`` returns the first
    result. ``expected_live`` makes the move conditional on LIVE still being that version.
    ``deployer`` rolls the version out to the serving system and reads it back.

    Raises ArtifactIntegrityError on a checksum mismatch, ConflictError when ``expected_live``
    no longer holds, PromotionError when the alias move or the rollout failed (LIVE and the
    serving system restored)."""
    if idempotency_key:
        earlier = session.execute(
            select(ModelPromotion).where(ModelPromotion.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if earlier is not None:
            return _result(earlier)

    meta = _model_meta(session, model_id)
    name = meta.mlflow_model_name
    previous = current_live(registry, name, live_alias)
    if expected_live is not None and previous != expected_live:
        raise ConflictError(
            "LIVE moved since this promotion was planned",
            model_id=model_id,
            expected_live=expected_live,
            actual_live=previous,
        )

    if previous == version:
        row = ModelPromotion(
            model_id=model_id,
            kind=kind.value,
            from_version=previous,
            to_version=version,
            status="NO_CHANGE",
            reason=reason or f"version {version} is already live",
            actor=actor,
            job_id=job_id,
            idempotency_key=idempotency_key,
        )
        session.add(row)
        session.commit()
        return _result(row)

    local, digest = verify_version_artifact(registry, name, version, workdir)
    target = DeploymentTarget(
        model=name,
        version=version,
        source=registry.get_version(name, version).source,
        artifact_dir=local,
    )

    row = ModelPromotion(
        model_id=model_id,
        kind=kind.value,
        from_version=previous,
        to_version=version,
        status="APPLIED",
        reason=reason,
        actor=actor,
        job_id=job_id,
        idempotency_key=idempotency_key,
        artifact_sha256=digest,
    )
    alias_moved = rollout_started = deployed = False
    serving_before: DeploymentTarget | None = None
    try:
        session.add(row)
        session.flush()
        serving_before = _serving_target(registry, deployer, name)
        registry.set_alias(name, live_alias, version)
        alias_moved = True
        now_live = registry.get_version_by_alias(name, live_alias)
        if now_live != version:
            raise PromotionError(
                "live alias did not read back as the promoted version",
                expected=version,
                actual=now_live,
            )
        rollout_started = True
        deployer.rollout(target, serving_before)
        deployed = True
        status_tag = registry.artifact_policy.status_tag
        registry.set_version_tags(name, version, {status_tag: ModelVersionStatus.LIVE.value})
        if previous is not None:
            registry.set_version_tags(
                name, previous, {status_tag: ModelVersionStatus.ARCHIVED.value}
            )
        _audit_move(
            session, kind, model_id=model_id, version=version, previous=previous, actor=actor,
            reason=reason, job_id=job_id, digest=digest,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        # A DeploymentError from rollout has already restored the serving system.
        if deployed or (rollout_started and not isinstance(exc, DeploymentError)):
            deployer.revert(name, serving_before)
        if alias_moved:
            _restore_alias(registry, name, live_alias, previous)
        _audit_failed_move(
            session, kind, model_id=model_id, version=version, previous=previous, actor=actor,
            reason=str(exc), job_id=job_id, digest=digest,
        )
        if isinstance(exc, PromotionError):
            raise
        raise PromotionError(
            f"promoting version {version} failed; LIVE restored to {previous}",
            model_id=model_id,
            version=version,
            previous=previous,
            cause=exc.message if isinstance(exc, AdaptationError) else str(exc),
        ) from exc

    metrics.PROMOTIONS.labels(kind=kind.value).inc()
    if kind is PromotionKind.ROLLBACK:
        metrics.ROLLBACK.labels("manual").inc()
    log_event(
        logger,
        "live alias moved",
        model_id=model_id,
        kind=kind.value,
        from_version=previous,
        to_version=version,
    )
    return _result(row)


def _audit_move(
    session: Session,
    kind: PromotionKind,
    *,
    model_id: str,
    version: str,
    previous: str | None,
    actor: str,
    reason: str,
    job_id: str | None,
    digest: str,
    status: str = "OK",
) -> None:
    record_audit(
        session,
        AuditAction.MODEL_ROLLED_BACK if kind is PromotionKind.ROLLBACK else AuditAction.MODEL_PROMOTED,
        component="promotion",
        actor=actor,
        job_id=job_id,
        model_id=model_id,
        model_version=version,
        decision=kind.value,
        reason=reason,
        status=status,
        metadata={"from_version": previous, "to_version": version, "artifact_sha256": digest},
    )


def _audit_failed_move(session: Session, kind: PromotionKind, **fields) -> None:
    """Best effort: a failed move is audited in its own commit, and a failure to write that row
    never replaces the promotion error the caller is about to see."""
    try:
        _audit_move(session, kind, status="FAILED", **fields)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log_event(logger, f"could not audit failed promotion: {exc}", level=logging.WARNING)


def last_applied_promotion(session: Session, model_id: str) -> ModelPromotion | None:
    return session.execute(
        select(ModelPromotion)
        .where(ModelPromotion.model_id == model_id, ModelPromotion.status == "APPLIED")
        .order_by(desc(ModelPromotion.id))
        .limit(1)
    ).scalar_one_or_none()


def rollback_model(
    session: Session,
    registry: ModelRegistryPort,
    handler: ModelHandlerPort,
    *,
    deployer: Deployer,
    model_id: str,
    live_alias: str,
    workdir: str,
    target_version: str | None = None,
    reason: str = "",
    actor: str = "system",
    idempotency_key: str | None = None,
) -> PromotionResult:
    """Move LIVE back. Without ``target_version`` it goes to the version LIVE held before the
    latest applied promotion. The target must exist, pass its checksum and load as a model."""
    meta = _model_meta(session, model_id)
    if target_version is None:
        last = last_applied_promotion(session, model_id)
        if last is None or last.from_version is None:
            raise ConflictError(
                "no earlier live version on record to roll back to", model_id=model_id
            )
        target_version = last.from_version

    registry.get_version(meta.mlflow_model_name, target_version)  # ModelNotFoundError if absent
    local, _ = verify_version_artifact(
        registry, meta.mlflow_model_name, target_version, os.path.join(workdir, "rollback")
    )
    if meta.framework is None:
        raise ArtifactError(
            f"model '{model_id}' has no framework on record; cannot check the rollback target",
            model_id=model_id,
            version=target_version,
        )
    try:
        handler.load(local, meta.framework)
    except Exception as exc:
        raise ArtifactError(
            f"rollback target version {target_version} does not load",
            model_id=model_id,
            version=target_version,
            cause=str(exc),
        ) from exc

    return promote_version(
        session,
        registry,
        deployer=deployer,
        model_id=model_id,
        version=target_version,
        kind=PromotionKind.ROLLBACK,
        live_alias=live_alias,
        workdir=os.path.join(workdir, "rollback"),
        reason=reason or f"rollback to version {target_version}",
        actor=actor,
        idempotency_key=idempotency_key,
    )
