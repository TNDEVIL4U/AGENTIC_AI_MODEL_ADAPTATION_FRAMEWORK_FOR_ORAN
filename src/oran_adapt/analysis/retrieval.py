"""Member 1 - retrieval: pull the model, historical baseline and drifted data for analysis.

PostgreSQL owns datasets/data versions/lineage (see db.models); this module is the only place
that turns a DriftEvent into the concrete data slices analysis needs. Rows are read through
datastore.access, so a version stored by reference is read from its object, and a version
larger than DATASET_ANALYSIS_MAX_ROWS is analyzed on an even sample of that many rows. It raises when the model
itself is unknown (a precondition the pipeline cannot proceed without) but returns ``None``
slices when the *data* is merely missing, because "no comparable data yet" is a legitimate
business outcome (INSUFFICIENT_DATA), not a system failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.enums import AssociationRole
from oran_adapt.core.errors import ModelNotFoundError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.datastore.access import DataAccess
from oran_adapt.db.models import (
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
    PerformanceRecord,
)


@dataclass
class DataSlice:
    """One dataset version as timestamped feature rows: all of them, or an even sample of
    DATASET_ANALYSIS_MAX_ROWS when the version is larger (``sampled``)."""

    data_version_id: int
    version: str
    kind: str
    row_count: int
    data_start: datetime | None
    data_end: datetime | None
    records: list[dict]  # each row: {"observed_at": datetime, **feature_payload}
    total_rows: int | None = None  # rows in the whole version (row_count counts ``records``)
    sampled: bool = False


@dataclass
class RetrievedContext:
    model: ModelMetadata
    historical: DataSlice | None
    drifted: DataSlice | None
    recent_performance: list[PerformanceRecord] = field(default_factory=list)


def _load_slice(
    session: Session, data_version: DataVersion | None, access: DataAccess
) -> DataSlice | None:
    if data_version is None:
        return None
    rows = access.sample(session, data_version)
    records = [{"observed_at": r.observed_at, **r.payload} for r in rows]
    total = data_version.row_count if data_version.row_count is not None else len(records)
    return DataSlice(
        data_version_id=data_version.id,
        version=data_version.version,
        kind=data_version.kind,
        row_count=len(records),
        data_start=data_version.data_start,
        data_end=data_version.data_end,
        records=records,
        total_rows=total,
        sampled=len(records) < total,
    )


def _latest_association(
    session: Session, model_id: str, role: AssociationRole
) -> ModelDataAssociation | None:
    return (
        session.execute(
            select(ModelDataAssociation)
            .where(
                ModelDataAssociation.model_id == model_id,
                ModelDataAssociation.role == role,
            )
            .order_by(ModelDataAssociation.created_at.desc(), ModelDataAssociation.id.desc())
        )
        .scalars()
        .first()
    )


def _historical_version(
    session: Session, model_id: str, live_version: str | None = None
) -> DataVersion | None:
    """The baseline is what the LIVE version was trained on. After an older version is reused,
    the newest TRAINING link belongs to a version that is no longer live, so the live version's
    own link is preferred; the newest link is the fallback for models onboarded without one."""
    assoc = None
    if live_version is not None:
        assoc = (
            session.execute(
                select(ModelDataAssociation)
                .where(
                    ModelDataAssociation.model_id == model_id,
                    ModelDataAssociation.model_version == live_version,
                    ModelDataAssociation.role == AssociationRole.TRAINING,
                )
                .order_by(ModelDataAssociation.id.desc())
            )
            .scalars()
            .first()
        )
    if assoc is None:
        assoc = _latest_association(session, model_id, AssociationRole.TRAINING)
    if assoc is None:
        return None
    return session.get(DataVersion, assoc.data_version_id)


def _drifted_version(session: Session, model_id: str, event: DriftEvent) -> DataVersion | None:
    if event.dataset_id and event.drifted_data_version:
        dataset = (
            session.execute(
                select(DatasetMetadata).where(DatasetMetadata.dataset_id == event.dataset_id)
            )
            .scalars()
            .first()
        )
        if dataset is not None:
            dv = (
                session.execute(
                    select(DataVersion).where(
                        DataVersion.dataset_id == dataset.id,
                        DataVersion.version == event.drifted_data_version,
                    )
                )
                .scalars()
                .first()
            )
            if dv is not None:
                return dv
    # Fallback: the most recently linked drift-observation data for this model.
    assoc = _latest_association(session, model_id, AssociationRole.DRIFT_OBSERVED)
    if assoc is None:
        return None
    return session.get(DataVersion, assoc.data_version_id)


def retrieve_context(
    session: Session,
    event: DriftEvent,
    *,
    performance_limit: int,
    live_version: str | None = None,
    access: DataAccess | None = None,
) -> RetrievedContext:
    """Assemble everything downstream analysis needs, or raise if the model is unknown.
    ``performance_limit`` (ANALYSIS_PERFORMANCE_HISTORY_LIMIT) caps the performance records;
    ``access`` reads the rows (database rows only when not given)."""
    access = access or DataAccess()
    model = (
        session.execute(select(ModelMetadata).where(ModelMetadata.model_id == event.model_id))
        .scalars()
        .first()
    )
    if model is None:
        raise ModelNotFoundError(
            f"Model '{event.model_id}' is not registered", model=event.model_id
        )

    performance = (
        session.execute(
            select(PerformanceRecord)
            .where(PerformanceRecord.model_id == event.model_id)
            .order_by(PerformanceRecord.recorded_at.desc())
            .limit(performance_limit)
        )
        .scalars()
        .all()
    )

    return RetrievedContext(
        model=model,
        historical=_load_slice(
            session, _historical_version(session, event.model_id, live_version), access
        ),
        drifted=_load_slice(session, _drifted_version(session, event.model_id, event), access),
        recent_performance=list(performance),
    )
