"""Moving a model's live alias: promoting a validated candidate, reusing an older version, or
rolling back. This is the only module that changes LIVE.

Every move is recorded as a ModelPromotion row whose ``from_version`` is what LIVE pointed at
before, so any move can be undone. A move is all-or-nothing across the database and MLflow: the
row is flushed first, then the alias is set and read back, then the version status tags are
updated and the transaction commits. If any step fails, the alias is put back to the previous
version (or removed, if there was none) and the database transaction is rolled back, so LIVE
never ends up pointing somewhere the history does not explain.

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

from oran_adapt.core.enums import ModelVersionStatus, PromotionKind
from oran_adapt.core.errors import (
    AdaptationError,
    ArtifactError,
    ConflictError,
    ModelNotFoundError,
    PromotionError,
)
from oran_adapt.core.integrity import sha256_path, verify_checksum
from oran_adapt.core.logging import log_event
from oran_adapt.db.models import AuditLog, ModelMetadata, ModelPromotion
from oran_adapt.registry.client import MlflowRegistry

logger = logging.getLogger(__name__)

CHECKSUM_TAG = "artifact.sha256"
STATUS_TAG = "oran.status"


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


def current_live(registry: MlflowRegistry, name: str, alias: str) -> str | None:
    try:
        return registry.get_version_by_alias(name, alias)
    except ModelNotFoundError:
        return None


def verify_version_artifact(
    registry: MlflowRegistry, name: str, version: str, workdir: str
) -> tuple[str, str]:
    """Download ``version``, check its checksum against the registration-time tag, and return
    ``(local_path, sha256)``. Records the checksum when the version has none yet."""
    info = registry.get_version(name, version)
    local = registry.download_artifacts(name, version, os.path.join(workdir, f"verify-{version}"))
    expected = (info.tags or {}).get(CHECKSUM_TAG)
    if expected:
        verify_checksum(local, expected, model=name, version=version)
        return local, expected
    digest = sha256_path(local)
    registry.set_version_tags(name, version, {CHECKSUM_TAG: digest})
    log_event(
        logger,
        "artifact had no recorded checksum; recorded it now (trust on first use)",
        model=name,
        version=version,
        sha256=digest,
    )
    return local, digest


def _restore_alias(registry: MlflowRegistry, name: str, alias: str, previous: str | None) -> None:
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
    registry: MlflowRegistry,
    *,
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

    Raises ArtifactIntegrityError on a checksum mismatch, ConflictError when ``expected_live``
    no longer holds, PromotionError when the alias move itself failed (LIVE restored)."""
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

    _, digest = verify_version_artifact(registry, name, version, workdir)

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
    alias_moved = False
    try:
        session.add(row)
        session.flush()
        registry.set_alias(name, live_alias, version)
        alias_moved = True
        now_live = registry.get_version_by_alias(name, live_alias)
        if now_live != version:
            raise PromotionError(
                "live alias did not read back as the promoted version",
                expected=version,
                actual=now_live,
            )
        registry.set_version_tags(name, version, {STATUS_TAG: ModelVersionStatus.LIVE.value})
        if previous is not None:
            registry.set_version_tags(
                name, previous, {STATUS_TAG: ModelVersionStatus.ARCHIVED.value}
            )
        session.add(
            AuditLog(
                job_id=job_id,
                action="MODEL_ROLLED_BACK" if kind is PromotionKind.ROLLBACK else "MODEL_PROMOTED",
                component="promotion",
                model_id=model_id,
                model_version=version,
                detail={
                    "kind": kind.value,
                    "from_version": previous,
                    "to_version": version,
                    "actor": actor,
                    "reason": reason,
                    "artifact_sha256": digest,
                },
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        if alias_moved:
            _restore_alias(registry, name, live_alias, previous)
        if isinstance(exc, PromotionError):
            raise
        raise PromotionError(
            f"promoting version {version} failed; LIVE restored to {previous}",
            model_id=model_id,
            version=version,
            previous=previous,
            cause=exc.message if isinstance(exc, AdaptationError) else str(exc),
        ) from exc

    log_event(
        logger,
        "live alias moved",
        model_id=model_id,
        kind=kind.value,
        from_version=previous,
        to_version=version,
    )
    return _result(row)


def last_applied_promotion(session: Session, model_id: str) -> ModelPromotion | None:
    return session.execute(
        select(ModelPromotion)
        .where(ModelPromotion.model_id == model_id, ModelPromotion.status == "APPLIED")
        .order_by(desc(ModelPromotion.id))
        .limit(1)
    ).scalar_one_or_none()


def rollback_model(
    session: Session,
    registry: MlflowRegistry,
    *,
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
    from oran_adapt.adaptation.loaders import load_native_model

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
    try:
        load_native_model(local, meta.framework)
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
        model_id=model_id,
        version=target_version,
        kind=PromotionKind.ROLLBACK,
        live_alias=live_alias,
        workdir=os.path.join(workdir, "rollback"),
        reason=reason or f"rollback to version {target_version}",
        actor=actor,
        idempotency_key=idempotency_key,
    )
