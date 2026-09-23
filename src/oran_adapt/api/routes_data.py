"""Data versioning API: datasets, immutable content-hashed data versions and their lineage.

Rows are sent as JSON records (one object per row). A version name is write-once: re-posting
the same content is an idempotent 200, different content under a taken name is a 409."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from oran_adapt.api.security import DATA_ROLES, DataEditor, require_roles
from oran_adapt.datastore import (
    get_or_create_dataset,
    get_version,
    ingest_version,
    lineage,
    list_datasets,
    list_versions,
)
from oran_adapt.datastore.versioning import get_dataset
from oran_adapt.db.base import session_scope

router = APIRouter(prefix="/datasets", tags=["data"])


class DatasetCreate(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    name: str | None = None
    description: str | None = None


class VersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=128)
    kind: Literal["HISTORICAL", "DRIFTED"] = "HISTORICAL"
    records: list[dict[str, Any]] = Field(min_length=1)
    timestamp_column: str | None = None
    start: datetime | None = None
    parent_version: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    role: Literal["TRAINING", "VALIDATION", "DRIFT_OBSERVED"] | None = None
    source: str | None = None


@router.post("", status_code=201, dependencies=[Depends(require_roles(*DATA_ROLES))])
def create_dataset(body: DatasetCreate, request: Request) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        ds = get_or_create_dataset(
            session, body.dataset_id, name=body.name, description=body.description
        )
        return {"dataset_id": ds.dataset_id, "name": ds.name, "description": ds.description}


@router.get("")
def get_datasets(request: Request) -> list[dict]:
    with session_scope(request.app.state.session_factory) as session:
        return list_datasets(session)


@router.post("/{dataset_id}/versions")
def create_version(
    dataset_id: str,
    body: VersionCreate,
    request: Request,
    response: Response,
    principal: DataEditor,
) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        info = ingest_version(
            session,
            dataset_id,
            body.version,
            pd.DataFrame.from_records(body.records),
            kind=body.kind,
            timestamp_column=body.timestamp_column,
            start=body.start,
            parent_version=body.parent_version,
            model_id=body.model_id,
            model_version=body.model_version,
            role=body.role,
            source=body.source or "api",
            actor=principal.name,
        )
        response.status_code = 201 if info.created else 200
        return info.as_dict()


@router.get("/{dataset_id}/versions")
def get_versions(dataset_id: str, request: Request) -> list[dict]:
    with session_scope(request.app.state.session_factory) as session:
        get_dataset(session, dataset_id)  # 404 for an unknown dataset, not an empty list
        return [v.as_dict() for v in list_versions(session, dataset_id)]


@router.get("/{dataset_id}/versions/{version}")
def get_one_version(dataset_id: str, version: str, request: Request) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        return get_version(session, dataset_id, version).as_dict()


@router.get("/{dataset_id}/versions/{version}/lineage")
def get_lineage(dataset_id: str, version: str, request: Request) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        return lineage(session, dataset_id, version)
