"""Model registry API: what models the framework manages, every MLflow version of each (with
its lineage tags) and which data versions each version was trained on / observed drift on.

Onboarding a *new* model object is deliberately not exposed over HTTP (it would mean
unpickling an uploaded file); use the CLI (``oran-adapt model onboard``) for that. Over HTTP a
model that is already in MLflow can be attached."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from oran_adapt.api.security import DATA_ROLES, Promoter, require_roles
from oran_adapt.core.errors import ModelBusyError, ModelNotFoundError
from oran_adapt.datastore import model_data_links
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import ModelLock, ModelMetadata, ModelPromotion, ModelVersionEvaluation
from oran_adapt.orchestrator.locks import add_lock, release_lock, take_over_expired_lock
from oran_adapt.registry.onboarding import attach_existing_model
from oran_adapt.registry.promotion import rollback_model

router = APIRouter(prefix="/models", tags=["models"])


class AttachModel(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    mlflow_model_name: str = Field(min_length=1)
    framework: str
    task_type: str
    target_column: str
    version: str | None = None
    dataset_id: str | None = None
    training_version: str | None = None


class RollbackRequest(BaseModel):
    # Omitted: go back to the version LIVE held before the latest promotion.
    target_version: str | None = Field(default=None, max_length=50)
    reason: str = Field(default="", max_length=1000)
    # Resending a request with the same key returns the first result instead of moving again.
    idempotency_key: str | None = Field(default=None, max_length=128)


def _require_model(session, model_id: str) -> ModelMetadata:
    meta = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    ).scalar_one_or_none()
    if meta is None:
        raise ModelNotFoundError(f"model '{model_id}' is not onboarded", model_id=model_id)
    return meta


def _summary(m: ModelMetadata) -> dict:
    return {
        "model_id": m.model_id,
        "mlflow_model_name": m.mlflow_model_name,
        "framework": m.framework,
        "task_type": m.task_type,
        "target_column": m.target_column,
    }


@router.get("")
def get_models(request: Request) -> list[dict]:
    with session_scope(request.app.state.session_factory) as session:
        rows = session.execute(select(ModelMetadata).order_by(ModelMetadata.id)).scalars().all()
        return [_summary(m) for m in rows]


@router.get("/{model_id}")
def get_model(model_id: str, request: Request) -> dict:
    state = request.app.state
    with session_scope(state.session_factory) as session:
        meta = session.execute(
            select(ModelMetadata).where(ModelMetadata.model_id == model_id)
        ).scalar_one_or_none()
        if meta is None:
            raise ModelNotFoundError(f"model '{model_id}' is not onboarded", model_id=model_id)
        body = _summary(meta)
        body["data_links"] = model_data_links(session, model_id)
    versions = state.registry.describe_versions(body["mlflow_model_name"])
    alias = state.settings.live_alias
    body["live_alias"] = alias
    body["live_version"] = next((v["version"] for v in versions if alias in v["aliases"]), None)
    body["versions"] = versions
    return body


@router.post("/attach", status_code=201, dependencies=[Depends(require_roles(*DATA_ROLES))])
def attach_model(body: AttachModel, request: Request) -> dict:
    state = request.app.state
    with session_scope(state.session_factory) as session:
        result = attach_existing_model(
            session,
            state.registry,
            live_alias=state.settings.live_alias,
            **body.model_dump(),
        )
        return result.as_dict()


@router.get("/{model_id}/versions")
def get_model_versions(model_id: str, request: Request) -> dict:
    """Every registered version with its aliases and tags, and which one is LIVE."""
    state = request.app.state
    with session_scope(state.session_factory) as session:
        name = _require_model(session, model_id).mlflow_model_name
    versions = state.registry.describe_versions(name)
    alias = state.settings.live_alias
    live = next((v["version"] for v in versions if alias in v["aliases"]), None)
    for v in versions:
        v["is_live"] = v["version"] == live
        v["lifecycle_status"] = v["tags"].get("oran.status", "LIVE" if v["is_live"] else None)
    return {"model_id": model_id, "live_version": live, "versions": versions}


@router.get("/{model_id}/evaluation")
def get_model_evaluation(model_id: str, request: Request) -> dict:
    """How every version scored on the current data in the latest job that scored them."""
    with session_scope(request.app.state.session_factory) as session:
        _require_model(session, model_id)
        latest = session.execute(
            select(ModelVersionEvaluation)
            .where(ModelVersionEvaluation.model_id == model_id)
            .order_by(desc(ModelVersionEvaluation.id))
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return {"model_id": model_id, "job_id": None, "evaluations": []}
        rows = (
            session.execute(
                select(ModelVersionEvaluation)
                .where(
                    ModelVersionEvaluation.model_id == model_id,
                    ModelVersionEvaluation.job_id == latest.job_id,
                )
                .order_by(ModelVersionEvaluation.id)
            )
            .scalars()
            .all()
        )
        return {
            "model_id": model_id,
            "job_id": latest.job_id,
            "evaluated_at": latest.created_at.isoformat(),
            "evaluations": [{**r.result, "reusable": r.reusable} for r in rows],
        }


@router.get("/{model_id}/promotions")
def get_model_promotions(model_id: str, request: Request) -> list[dict]:
    """Every move of the live alias, newest first: promotions, reuses and rollbacks."""
    with session_scope(request.app.state.session_factory) as session:
        _require_model(session, model_id)
        rows = (
            session.execute(
                select(ModelPromotion)
                .where(ModelPromotion.model_id == model_id)
                .order_by(desc(ModelPromotion.id))
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "from_version": r.from_version,
                "to_version": r.to_version,
                "status": r.status,
                "reason": r.reason,
                "actor": r.actor,
                "job_id": r.job_id,
                "artifact_sha256": r.artifact_sha256,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.post("/{model_id}/rollback")
def rollback(
    model_id: str,
    body: RollbackRequest,
    request: Request,
    principal: Promoter,
) -> dict:
    """Move LIVE back to an earlier version. Holds the model's lock, so it cannot interleave
    with a running adaptation job (409 MODEL_BUSY while one runs)."""
    state = request.app.state
    settings = state.settings
    holder = f"rollback-{uuid.uuid4().hex}"
    with session_scope(state.session_factory) as session:
        _require_model(session, model_id)
        try:
            if not take_over_expired_lock(session, model_id, holder, settings.model_lock_ttl_s):
                add_lock(session, model_id, holder, settings.model_lock_ttl_s)
                session.flush()
        except IntegrityError:
            session.rollback()
            current = session.get(ModelLock, model_id)
            raise ModelBusyError(
                f"model '{model_id}' has an adaptation job running; retry the rollback later",
                model_id=model_id,
                running_job_id=current.job_id if current else None,
            ) from None
    try:
        with session_scope(state.session_factory) as session:
            result = rollback_model(
                session,
                state.registry,
                model_id=model_id,
                live_alias=settings.live_alias,
                workdir=os.path.join(settings.artifact_workdir, holder),
                target_version=body.target_version,
                reason=body.reason,
                actor=principal.name,
                idempotency_key=f"rollback:{model_id}:{body.idempotency_key}"
                if body.idempotency_key
                else None,
            )
            return result.to_dict()
    finally:
        with session_scope(state.session_factory) as session:
            release_lock(session, model_id, holder)
