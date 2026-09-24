"""Data versioning: datasets, immutable content-addressed data versions, model<->data links and
lineage - all stored in the framework's own database (the same tables Member 1 reads from).

A data version is identified by (dataset_id, version) and fingerprinted by a SHA-256 over its
canonical content. Version names are immutable: re-ingesting the same name with the same rows is
an idempotent no-op, with different rows a DataVersionConflictError. A version may name a parent
(the version it was derived from), which is how the pipeline's post-adaptation training
snapshots form a lineage chain: v1's training data -> (+ drifted data) -> v2's training data ...

MLflow stays the only authority for models; the model_version values stored here are
references to MLflow versions, and the registry side carries the matching `data.*` tags (see
MlflowRegistry.log_model), so lineage can be followed from either end.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.audit import record_audit
from oran_adapt.core.enums import AssociationRole, AuditAction, DataKind
from oran_adapt.core.errors import (
    ArtifactError,
    DatasetNotFoundError,
    DataVersionConflictError,
    ModelNotFoundError,
)
from oran_adapt.db.models import (
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
)

DEFAULT_ROW_SPACING = timedelta(minutes=1)


@dataclass
class VersionInfo:
    dataset_id: str
    version: str
    data_version_id: int
    kind: str
    row_count: int
    content_hash: str | None
    parent_version: str | None
    data_start: datetime | None
    data_end: datetime | None
    columns: dict[str, str] = field(default_factory=dict)
    created: bool = True  # False when the ingest was an idempotent replay
    source: str | None = None
    schema_hash: str | None = None
    storage_uri: str | None = None
    status: str | None = None
    cdc_range: dict | None = None
    source_tx: list | None = None
    created_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "data_version_id": self.data_version_id,
            "kind": self.kind,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
            "parent_version": self.parent_version,
            "data_start": self.data_start.isoformat() if self.data_start else None,
            "data_end": self.data_end.isoformat() if self.data_end else None,
            "columns": self.columns,
            "created": self.created,
            "source": self.source,
            "schema_hash": self.schema_hash,
            "storage_uri": self.storage_uri,
            "status": self.status,
            "cdc_range": self.cdc_range,
            "source_tx": self.source_tx,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --------------------------------------------------------------------------- hashing
def _canonical_records(frame: pd.DataFrame, observed_at: list[datetime]) -> list[dict]:
    # JSON round-trip turns numpy scalars into plain Python values (what the JSON column stores).
    rows = json.loads(frame.to_json(orient="records", double_precision=15))
    return [
        {"observed_at": ts.isoformat(), **{k: row[k] for k in sorted(row)}}
        for ts, row in zip(observed_at, rows, strict=True)
    ]


def content_hash(frame: pd.DataFrame, observed_at: list[datetime]) -> str:
    """SHA-256 of the rows exactly as they would be stored: column order does not matter, row
    order and timestamps do."""
    canonical = json.dumps(_canonical_records(frame, observed_at), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def schema_hash(columns: dict[str, str]) -> str:
    """SHA-256 of a column -> dtype map (column order does not matter)."""
    return hashlib.sha256(json.dumps(columns, sort_keys=True).encode()).hexdigest()


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _timestamps(
    frame: pd.DataFrame, timestamp_column: str | None, start: datetime | None, spacing: timedelta
) -> tuple[pd.DataFrame, list[datetime]]:
    if timestamp_column:
        if timestamp_column not in frame.columns:
            raise ArtifactError(
                f"timestamp column {timestamp_column!r} not in data",
                available_columns=list(frame.columns),
            )
        stamps = pd.to_datetime(frame[timestamp_column], utc=True)
        return frame.drop(columns=[timestamp_column]), [ts.to_pydatetime() for ts in stamps]
    base = _as_utc(start) if start else datetime.now(UTC)
    return frame, [base + i * spacing for i in range(len(frame))]


# --------------------------------------------------------------------------- datasets
def get_dataset(session: Session, dataset_id: str) -> DatasetMetadata:
    dataset = session.execute(
        select(DatasetMetadata).where(DatasetMetadata.dataset_id == dataset_id)
    ).scalar_one_or_none()
    if dataset is None:
        raise DatasetNotFoundError(f"dataset '{dataset_id}' not found", dataset_id=dataset_id)
    return dataset


def get_or_create_dataset(
    session: Session,
    dataset_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: dict | None = None,
) -> DatasetMetadata:
    dataset = session.execute(
        select(DatasetMetadata).where(DatasetMetadata.dataset_id == dataset_id)
    ).scalar_one_or_none()
    if dataset is not None:
        return dataset
    dataset = DatasetMetadata(
        dataset_id=dataset_id, name=name or dataset_id, description=description, schema=schema or {}
    )
    session.add(dataset)
    session.flush()
    return dataset


def list_datasets(session: Session) -> list[dict]:
    datasets = session.execute(select(DatasetMetadata).order_by(DatasetMetadata.id)).scalars().all()
    return [
        {
            "dataset_id": d.dataset_id,
            "name": d.name,
            "description": d.description,
            "versions": [v.version for v in sorted(d.versions, key=lambda v: v.id)],
        }
        for d in datasets
    ]


# --------------------------------------------------------------------------- versions
def _info(dv: DataVersion, dataset_id: str, *, created: bool, session: Session) -> VersionInfo:
    parent = session.get(DataVersion, dv.parent_version_id) if dv.parent_version_id else None
    extra = dv.extra or {}
    return VersionInfo(
        dataset_id=dataset_id,
        version=dv.version,
        data_version_id=dv.id,
        kind=dv.kind,
        row_count=dv.row_count,
        content_hash=dv.content_hash,
        parent_version=parent.version if parent else None,
        data_start=dv.data_start,
        data_end=dv.data_end,
        columns=extra.get("columns", {}),
        created=created,
        source=dv.source or extra.get("source"),
        schema_hash=dv.schema_hash,
        storage_uri=dv.storage_uri,
        status=dv.status,
        cdc_range=dv.cdc_range,
        source_tx=dv.source_tx,
        created_at=dv.ingested_at,
    )


def _find_version(session: Session, dataset: DatasetMetadata, version: str) -> DataVersion | None:
    return session.execute(
        select(DataVersion).where(
            DataVersion.dataset_id == dataset.id, DataVersion.version == version
        )
    ).scalar_one_or_none()


def get_version(session: Session, dataset_id: str, version: str) -> VersionInfo:
    dataset = get_dataset(session, dataset_id)
    dv = _find_version(session, dataset, version)
    if dv is None:
        raise DatasetNotFoundError(
            f"dataset '{dataset_id}' has no version '{version}'",
            dataset_id=dataset_id,
            version=version,
        )
    return _info(dv, dataset_id, created=False, session=session)


def list_versions(session: Session, dataset_id: str) -> list[VersionInfo]:
    dataset = get_dataset(session, dataset_id)
    rows = session.execute(
        select(DataVersion).where(DataVersion.dataset_id == dataset.id).order_by(DataVersion.id)
    ).scalars().all()
    return [_info(dv, dataset_id, created=False, session=session) for dv in rows]


def ingest_version(
    session: Session,
    dataset_id: str,
    version: str,
    frame: pd.DataFrame,
    *,
    kind: DataKind | str,
    timestamp_column: str | None = None,
    start: datetime | None = None,
    spacing: timedelta = DEFAULT_ROW_SPACING,
    parent_version: str | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    role: AssociationRole | str | None = None,
    source: str | None = None,
    dataset_name: str | None = None,
    actor: str = "system",
    record_keys: list[str | None] | None = None,
    deleted_keys: list[str] | None = None,
    cdc_range: dict | None = None,
    source_tx: list | None = None,
) -> VersionInfo:
    """Store ``frame`` as data version ``version`` of ``dataset_id`` (created on first use).

    Row timestamps come from ``timestamp_column`` when given, otherwise ``start`` +
    i * ``spacing``. With ``model_id``/``model_version``/``role`` the version is also linked to
    that model version (e.g. role TRAINING for its training data, DRIFT_OBSERVED for data a
    drift detector flagged). Idempotent for an identical re-ingest; raises
    DataVersionConflictError if the name is taken by different content.

    CDC-derived versions also pass ``record_keys`` (the source primary key of each row),
    ``deleted_keys`` (source rows deleted in this window; such a version may have no rows),
    ``cdc_range`` (offsets covered) and ``source_tx`` (source transaction ids)."""
    if frame.empty and not deleted_keys:
        raise ArtifactError("refusing to ingest an empty data version", version=version)
    if record_keys is not None and len(record_keys) != len(frame):
        raise ArtifactError("record_keys must have one key per row", version=version)
    kind = DataKind(kind)
    dataset = get_or_create_dataset(session, dataset_id, name=dataset_name)
    features, observed_at = _timestamps(frame, timestamp_column, start, spacing)
    digest = content_hash(features, observed_at)
    if record_keys is not None or deleted_keys:
        # Keys and deletions are part of what the version says, so part of its identity.
        digest = hashlib.sha256(
            json.dumps([digest, record_keys, sorted(deleted_keys or [])]).encode()
        ).hexdigest()

    existing = _find_version(session, dataset, version)
    if existing is not None:
        if existing.content_hash != digest:
            raise DataVersionConflictError(
                f"dataset '{dataset_id}' already has a different version '{version}'",
                dataset_id=dataset_id,
                version=version,
                existing_hash=existing.content_hash,
                new_hash=digest,
            )
        if model_id and model_version and role:
            link_model_data(session, model_id, model_version, existing.id, role)
        return _info(existing, dataset_id, created=False, session=session)

    parent_id = None
    if parent_version:
        parent = _find_version(session, dataset, parent_version)
        if parent is None:
            raise DatasetNotFoundError(
                f"parent version '{parent_version}' not found in dataset '{dataset_id}'",
                dataset_id=dataset_id,
            )
        parent_id = parent.id

    columns = {c: str(t) for c, t in features.dtypes.items()}
    extra: dict = {"columns": columns, "source": source}
    if deleted_keys:
        extra["deleted_keys"] = sorted(deleted_keys)
    dv = DataVersion(
        dataset_id=dataset.id,
        version=version,
        kind=kind,
        parent_version_id=parent_id,
        data_start=min(observed_at) if observed_at else None,
        data_end=max(observed_at) if observed_at else None,
        row_count=len(features),
        content_hash=digest,
        extra=extra,
        source=source,
        schema_hash=schema_hash(columns),
        status="AVAILABLE",
        cdc_range=cdc_range,
        source_tx=source_tx,
    )
    session.add(dv)
    session.flush()
    dv.storage_uri = f"db://data_record?data_version_id={dv.id}"
    payloads = (
        json.loads(features.to_json(orient="records", double_precision=15)) if len(features) else []
    )
    keys = record_keys if record_keys is not None else [None] * len(payloads)
    session.add_all(
        DataRecord(data_version_id=dv.id, observed_at=ts, payload=row, record_key=key)
        for ts, row, key in zip(observed_at, payloads, keys, strict=True)
    )
    if model_id and model_version and role:
        link_model_data(session, model_id, model_version, dv.id, role)
    record_audit(
        session,
        AuditAction.DATA_VERSION_CREATED,
        component="datastore",
        actor=actor,
        model_id=model_id,
        model_version=str(model_version) if model_version else None,
        reason=source,
        metadata={
            "dataset_id": dataset_id,
            "version": version,
            "kind": kind.value,
            "row_count": len(features),
            "content_hash": digest,
            "parent_version": parent_version,
            "role": str(role) if role else None,
            "schema_hash": dv.schema_hash,
            "cdc_range": cdc_range,
        },
    )
    session.flush()
    return _info(dv, dataset_id, created=True, session=session)


def link_model_data(
    session: Session,
    model_id: str,
    model_version: str,
    data_version_id: int,
    role: AssociationRole | str,
) -> None:
    """Idempotently record that ``model_id`` version ``model_version`` used this data version in
    ``role``. Raises ModelNotFoundError if ``model_id`` is not onboarded."""
    role = AssociationRole(role)
    known = session.execute(
        select(ModelMetadata.id).where(ModelMetadata.model_id == model_id)
    ).scalar_one_or_none()
    if known is None:
        raise ModelNotFoundError(f"model '{model_id}' is not onboarded", model_id=model_id)
    exists = session.execute(
        select(ModelDataAssociation).where(
            ModelDataAssociation.model_id == model_id,
            ModelDataAssociation.model_version == str(model_version),
            ModelDataAssociation.data_version_id == data_version_id,
            ModelDataAssociation.role == role,
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(
            ModelDataAssociation(
                model_id=model_id,
                model_version=str(model_version),
                data_version_id=data_version_id,
                role=role,
            )
        )
        session.flush()


def model_data_links(session: Session, model_id: str) -> list[dict]:
    rows = session.execute(
        select(ModelDataAssociation, DataVersion, DatasetMetadata)
        .join(DataVersion, DataVersion.id == ModelDataAssociation.data_version_id)
        .join(DatasetMetadata, DatasetMetadata.id == DataVersion.dataset_id)
        .where(ModelDataAssociation.model_id == model_id)
        .order_by(ModelDataAssociation.id)
    ).all()
    return [
        {
            "model_version": assoc.model_version,
            "role": assoc.role,
            "dataset_id": ds.dataset_id,
            "data_version": dv.version,
            "kind": dv.kind,
            "row_count": dv.row_count,
            "content_hash": dv.content_hash,
        }
        for assoc, dv, ds in rows
    ]


def lineage(session: Session, dataset_id: str, version: str) -> dict:
    """The version, its ancestor chain (nearest parent first) and every model version linked to
    any of them."""
    dataset = get_dataset(session, dataset_id)
    dv = _find_version(session, dataset, version)
    if dv is None:
        raise DatasetNotFoundError(
            f"dataset '{dataset_id}' has no version '{version}'", dataset_id=dataset_id
        )
    chain = []
    seen: set[int] = set()
    node: DataVersion | None = dv
    while node is not None and node.id not in seen:
        seen.add(node.id)
        chain.append(node)
        node = session.get(DataVersion, node.parent_version_id) if node.parent_version_id else None

    links = session.execute(
        select(ModelDataAssociation).where(ModelDataAssociation.data_version_id.in_(seen))
    ).scalars().all()
    by_version = {n.id: n.version for n in chain}
    return {
        "dataset_id": dataset_id,
        "version": _info(dv, dataset_id, created=False, session=session).as_dict(),
        "ancestors": [
            {"version": n.version, "kind": n.kind, "row_count": n.row_count,
             "content_hash": n.content_hash}
            for n in chain[1:]
        ],
        "models": [
            {"model_id": a.model_id, "model_version": a.model_version, "role": a.role,
             "data_version": by_version[a.data_version_id]}
            for a in links
        ],
    }


# --------------------------------------------------------------------------- pipeline hook
def snapshot_training_data(
    session: Session,
    *,
    model_id: str,
    model_version: str,
    source_version_ids: list[int],
    parent_version_id: int | None,
    exclude_record_ids: Collection[int] = (),
    job_ref: str | None = None,
) -> VersionInfo:
    """After the pipeline registers a new model version, freeze exactly the rows it was trained
    on (every source version minus the validation hold-out ``exclude_record_ids``, timestamps
    preserved) as one new HISTORICAL data version, derived
    from the previous baseline, and link it to the new model version as TRAINING data. The next
    drift event for this model is then compared against the data the *live* model actually
    learned from, instead of the stale pre-adaptation baseline."""
    sources = [session.get(DataVersion, i) for i in source_version_ids]
    if not sources or any(s is None for s in sources):
        raise ArtifactError("cannot snapshot training data: unknown source data version",
                            source_version_ids=source_version_ids)
    anchor = session.get(DataVersion, parent_version_id) if parent_version_id else sources[0]
    dataset = session.get(DatasetMetadata, anchor.dataset_id)

    rows = session.execute(
        select(DataRecord)
        .where(DataRecord.data_version_id.in_(source_version_ids))
        .order_by(DataRecord.observed_at, DataRecord.id)
    ).scalars().all()
    excluded = set(exclude_record_ids)
    rows = [r for r in rows if r.id not in excluded]
    frame = pd.DataFrame([r.payload for r in rows])
    stamps = [_as_utc(r.observed_at) for r in rows]
    frame["__observed_at"] = stamps

    source_names = [s.version for s in sources]
    return ingest_version(
        session,
        dataset.dataset_id,
        f"train-{model_id}-v{model_version}",
        frame,
        kind=DataKind.HISTORICAL,
        timestamp_column="__observed_at",
        parent_version=anchor.version if parent_version_id else None,
        model_id=model_id,
        model_version=model_version,
        role=AssociationRole.TRAINING,
        source=f"adaptation snapshot of {'+'.join(source_names)}"
        + (f" minus {len(excluded)} held-out rows" if excluded else "")
        + (f" (job {job_ref})" if job_ref else ""),
    )
