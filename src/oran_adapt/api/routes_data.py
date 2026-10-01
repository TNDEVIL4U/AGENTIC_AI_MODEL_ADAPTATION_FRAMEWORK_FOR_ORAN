"""Data versioning API: datasets, immutable content-hashed data versions and their lineage.

Rows are sent as JSON records (one object per row), or referenced where they already are
(``storage_uri``: a file, URL or object-store URI read by a configured dataset adapter,
DATASET_BACKENDS) without being copied into the database. A version name is write-once:
re-posting the same content is an idempotent 200, different content under a taken name is a
409. The same rows hash the same whichever way they were sent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from oran_adapt.api.security import DATA, DataEditor, require
from oran_adapt.core.errors import DatasetNotFoundError
from oran_adapt.datastore import (
    get_or_create_dataset,
    get_version,
    ingest_version,
    lineage,
    list_datasets,
    list_versions,
)
from oran_adapt.datastore.access import DataAccess
from oran_adapt.datastore.current_data import get_current_data, list_current_data
from oran_adapt.datastore.versioning import get_dataset, storage_of, version_row
from oran_adapt.db.base import session_scope

router = APIRouter(prefix="/datasets", tags=["data"])
# CurrentData: the cleaned, versioned rows each adaptation job decided on.
current_router = APIRouter(prefix="/current-data", tags=["data"])


class DatasetCreate(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    name: str | None = None
    description: str | None = None


class VersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=128)
    kind: Literal["HISTORICAL", "DRIFTED"] = "HISTORICAL"
    records: list[dict[str, Any]] | None = Field(None, min_length=1)
    storage_uri: str | None = Field(None, min_length=1, max_length=500)
    format: Literal["csv", "jsonl", "parquet"] | None = None
    timestamp_column: str | None = None
    start: datetime | None = None
    parent_version: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    role: Literal["TRAINING", "VALIDATION", "DRIFT_OBSERVED"] | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> VersionCreate:
        if (self.records is None) == (self.storage_uri is None):
            raise ValueError("send exactly one of records or storage_uri")
        if self.storage_uri is None and self.format is not None:
            raise ValueError("format applies only with storage_uri")
        if self.storage_uri is not None and self.start is not None:
            raise ValueError("start applies only to records; a referenced object needs "
                             "timestamp_column")
        return self


def _access(request: Request) -> DataAccess:
    return request.app.state.data_access


@router.post("", status_code=201, dependencies=[Depends(require(DATA))])
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
        if body.storage_uri is not None:
            info = _access(request).register(
                session,
                dataset_id,
                body.version,
                body.storage_uri,
                kind=body.kind,
                fmt=body.format,
                timestamp_column=body.timestamp_column,
                parent_version=body.parent_version,
                model_id=body.model_id,
                model_version=body.model_version,
                role=body.role,
                source=body.source,
                actor=principal.name,
            )
            response.status_code = 201 if info.created else 200
            return info.as_dict()
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


@router.get("/{dataset_id}/versions/{version}/rows")
def get_rows(
    dataset_id: str,
    version: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=1000),
) -> dict:
    """A page of the version's rows, wherever they are stored."""
    limit = limit or request.app.state.settings.api_pagination_default_limit
    with session_scope(request.app.state.session_factory) as session:
        dv = version_row(session, dataset_id, version)
        rows = _access(request).page(session, dv, offset=offset, limit=limit)
        return {
            "dataset_id": dataset_id,
            "version": version,
            "storage": storage_of(dv),
            "total_rows": dv.row_count,
            "offset": offset,
            "limit": limit,
            "rows": [
                {"observed_at": r.observed_at.isoformat(), "record_key": r.record_key,
                 "payload": r.payload}
                for r in rows
            ],
        }


@router.post("/{dataset_id}/versions/{version}/verify", dependencies=[Depends(require(DATA))])
def verify_version(dataset_id: str, version: str, request: Request) -> dict:
    """Re-read the version's rows and recompute its content hash (for a referenced object,
    also compare its fingerprint): whether the stored data still is what was registered."""
    with session_scope(request.app.state.session_factory) as session:
        return _access(request).verify(session, version_row(session, dataset_id, version))


@router.get("/{dataset_id}/versions/{version}/lineage")
def get_lineage(dataset_id: str, version: str, request: Request) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        return lineage(session, dataset_id, version)


@router.post("/{dataset_id}/cdc/materialize")
def materialize_cdc_events(
    dataset_id: str, request: Request, response: Response, principal: DataEditor
) -> dict:
    """Fold the dataset's pending CDC events into a new immutable CDC data version."""
    from oran_adapt.cdc.materialize import materialize_cdc

    with session_scope(request.app.state.session_factory) as session:
        info = materialize_cdc(
            session,
            dataset_id,
            max_tx_ids=request.app.state.settings.cdc_max_tx_ids_per_version,
            actor=principal.name,
        )
    if info is None:
        response.status_code = 200
        return {"dataset_id": dataset_id, "materialized": False, "reason": "no pending events"}
    response.status_code = 201
    return {"materialized": True, **info.as_dict()}


@current_router.get("")
def get_current_data_list(request: Request, model_id: str | None = None) -> list[dict]:
    with session_scope(request.app.state.session_factory) as session:
        return list_current_data(
            session,
            limit=request.app.state.settings.api_pagination_default_limit,
            model_id=model_id,
        )


@current_router.get("/{current_data_id}")
def get_one_current_data(current_data_id: str, request: Request) -> dict:
    with session_scope(request.app.state.session_factory) as session:
        found = get_current_data(session, current_data_id)
    if found is None:
        raise DatasetNotFoundError(
            f"current data '{current_data_id}' not found", current_data_id=current_data_id
        )
    return found
