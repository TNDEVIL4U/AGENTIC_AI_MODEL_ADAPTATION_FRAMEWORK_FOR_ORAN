"""CurrentData: the cleaned, versioned data an adaptation job makes its decisions on.

    historical + drifted/CDC versions -> resolve conflicts (same record_key: newest version
    wins; keys a newer CDC version deleted are dropped) -> drop exact duplicates -> schema
    validation (required columns present and non-null) -> timestamp ordering

``clean_records`` does that on the job's source rows. The pipeline then holds out the newest
rows as the evaluation window, and ``persist_current_data`` freezes that window as an immutable
CURRENT data version (named by its content hash, so the same rows are stored once) plus a
``current_data`` row carrying the lineage: source versions, time range, schema, hash, and how
many rows each cleaning step removed. Every version score and the validation gate of the job
refer to its ``current_data_id``.

Uploads without record keys pass through conflict resolution unchanged, so for them cleaning
only removes exact duplicates and rows missing required values.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.enums import DataKind
from oran_adapt.datastore.versioning import content_hash, ingest_version, schema_hash
from oran_adapt.db.models import CurrentData, DataRecord, DatasetMetadata, DataVersion


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


@dataclass
class CleanResult:
    records: list[DataRecord]
    quality: dict[str, int] = field(default_factory=dict)


def clean_records(
    session: Session, records: Sequence[DataRecord], *, required_columns: Sequence[str] = ()
) -> CleanResult:
    """The cleaning steps above, applied to ``records`` (rows of one or more data versions)."""
    version_ids = {r.data_version_id for r in records}
    versions = session.execute(
        select(DataVersion).where(DataVersion.id.in_(version_ids))
    ).scalars().all() if version_ids else []
    deleted_in: dict[str, int] = {}  # record_key -> newest version that deleted it
    for v in versions:
        for key in (v.extra or {}).get("deleted_keys", []):
            deleted_in[key] = max(deleted_in.get(key, 0), v.id)

    def rank(r: DataRecord) -> tuple:
        return (r.data_version_id, _utc(r.observed_at), r.id)

    newest: dict[str, DataRecord] = {}
    for r in records:
        if r.record_key is not None:
            cur = newest.get(r.record_key)
            if cur is None or rank(r) > rank(cur):
                newest[r.record_key] = r

    conflicts = deleted = duplicates = rejected = 0
    seen: set[tuple[str, str]] = set()
    kept: list[DataRecord] = []
    for r in records:
        if r.record_key is not None:
            if newest[r.record_key] is not r:
                conflicts += 1
                continue
            if deleted_in.get(r.record_key, 0) > r.data_version_id:
                deleted += 1
                continue
        signature = (_utc(r.observed_at).isoformat(), json.dumps(r.payload, sort_keys=True))
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        if any(r.payload.get(c) is None for c in required_columns):
            rejected += 1
            continue
        kept.append(r)
    kept.sort(key=lambda r: (_utc(r.observed_at), r.id))
    return CleanResult(
        records=kept,
        quality={
            "input_rows": len(records),
            "conflicts_resolved": conflicts,
            "deleted_removed": deleted,
            "duplicates_removed": duplicates,
            "schema_rejected": rejected,
            "output_rows": len(kept),
        },
    )


def _as_dict(row: CurrentData, dv: DataVersion, dataset_id: str) -> dict:
    return {
        "current_data_id": row.current_data_id,
        "dataset_id": dataset_id,
        "data_version": dv.version,
        "data_version_id": row.data_version_id,
        "model_id": row.model_id,
        "job_id": row.job_id,
        "source_versions": row.source_versions,
        "data_start": row.data_start.isoformat() if row.data_start else None,
        "data_end": row.data_end.isoformat() if row.data_end else None,
        "row_count": row.row_count,
        "schema": row.schema,
        "schema_hash": row.schema_hash,
        "content_hash": row.content_hash,
        "quality": row.quality,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def persist_current_data(
    session: Session,
    *,
    model_id: str,
    job_id: str | None,
    records: Sequence[DataRecord],
    source_version_ids: Sequence[int],
    quality: dict,
    actor: str = "system",
) -> dict | None:
    """Freeze ``records`` (the evaluation window) as a CURRENT data version of the first
    source's dataset and record the CurrentData row. Returns its description, or None when
    there are no rows to freeze. The caller commits."""
    if not records:
        return None
    sources = [session.get(DataVersion, i) for i in source_version_ids]
    sources = [s for s in sources if s is not None]
    dataset = session.get(DatasetMetadata, sources[0].dataset_id)

    frame = pd.DataFrame([r.payload for r in records])
    stamps = [_utc(r.observed_at) for r in records]
    keys = [r.record_key for r in records]
    row_digest = content_hash(frame, stamps)
    frame["__observed_at"] = stamps
    info = ingest_version(
        session,
        dataset.dataset_id,
        f"current-{row_digest[:16]}",
        frame,
        kind=DataKind.CURRENT,
        timestamp_column="__observed_at",
        source=f"CurrentData for {model_id}" + (f" (job {job_id})" if job_id else ""),
        actor=actor,
        record_keys=keys if any(k is not None for k in keys) else None,
    )
    columns = info.columns
    row = CurrentData(
        current_data_id=uuid.uuid4().hex,
        data_version_id=info.data_version_id,
        model_id=model_id,
        job_id=job_id,
        source_versions=[
            {
                "data_version_id": s.id,
                "version": s.version,
                "kind": s.kind,
                "content_hash": s.content_hash,
            }
            for s in sources
        ],
        data_start=min(stamps),
        data_end=max(stamps),
        row_count=len(records),
        schema=columns,
        schema_hash=schema_hash(columns),
        content_hash=info.content_hash or row_digest,
        quality=quality,
    )
    session.add(row)
    session.flush()
    dv = session.get(DataVersion, info.data_version_id)
    return _as_dict(row, dv, dataset.dataset_id)


def get_current_data(session: Session, current_data_id: str) -> dict | None:
    row = session.execute(
        select(CurrentData).where(CurrentData.current_data_id == current_data_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    dv = session.get(DataVersion, row.data_version_id)
    dataset = session.get(DatasetMetadata, dv.dataset_id)
    return _as_dict(row, dv, dataset.dataset_id)


def list_current_data(session: Session, *, model_id: str | None = None, limit: int = 50) -> list:
    query = select(CurrentData).order_by(CurrentData.id.desc()).limit(limit)
    if model_id:
        query = query.where(CurrentData.model_id == model_id)
    rows = session.execute(query).scalars().all()
    out = []
    for row in rows:
        dv = session.get(DataVersion, row.data_version_id)
        dataset = session.get(DatasetMetadata, dv.dataset_id)
        out.append(_as_dict(row, dv, dataset.dataset_id))
    return out
