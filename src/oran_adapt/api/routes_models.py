"""Model registry API: what models the framework manages, every MLflow version of each (with
its lineage tags) and which data versions each version was trained on / observed drift on.

Onboarding a *new* model object is deliberately not exposed over HTTP (it would mean
unpickling an uploaded file); use the CLI (``oran-adapt model onboard``) for that. Over HTTP a
model that is already in MLflow can be attached."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from oran_adapt.core.errors import ModelNotFoundError
from oran_adapt.datastore import model_data_links
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import ModelMetadata
from oran_adapt.registry.onboarding import attach_existing_model

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


@router.post("/attach", status_code=201)
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
