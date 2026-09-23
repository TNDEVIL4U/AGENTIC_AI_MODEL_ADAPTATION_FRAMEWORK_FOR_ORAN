"""Member 3 - training data: pull the real feature/target rows a data version points at, so
retraining and fine-tuning engines fit on the same PostgreSQL-owned data Member 1 analyzed -
never synthetic or re-derived data."""

from __future__ import annotations

import math

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.errors import ArtifactError
from oran_adapt.db.models import DataRecord


def load_data_version_frame(session: Session, data_version_id: int) -> pd.DataFrame:
    rows = (
        session.execute(
            select(DataRecord)
            .where(DataRecord.data_version_id == data_version_id)
            .order_by(DataRecord.observed_at)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise ArtifactError(
            "no data records found for data version", data_version_id=data_version_id
        )
    return pd.DataFrame([r.payload for r in rows])


def build_training_frame(session: Session, data_version_ids: list[int]) -> pd.DataFrame:
    frames = [load_data_version_frame(session, dv_id) for dv_id in data_version_ids]
    return pd.concat(frames, ignore_index=True)


def load_records(session: Session, data_version_ids: list[int]) -> list[DataRecord]:
    """All records of the given data versions, oldest first (id breaks timestamp ties)."""
    rows = (
        session.execute(
            select(DataRecord)
            .where(DataRecord.data_version_id.in_(data_version_ids))
            .order_by(DataRecord.observed_at, DataRecord.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise ArtifactError(
            "no data records found for data versions", data_version_ids=data_version_ids
        )
    return list(rows)


def records_frame(records: list[DataRecord]) -> pd.DataFrame:
    return pd.DataFrame([r.payload for r in records])


def holdout_size(n_rows: int, fraction: float, min_rows: int) -> int:
    """How many of the newest ``n_rows`` to hold back for validation: ``fraction`` of them, at
    least ``min_rows`` so the validation gate can score them, but always leaving one row to
    train on. 0 when there is nothing to split (validation then refuses to score)."""
    return max(0, min(max(math.ceil(n_rows * fraction), min_rows), n_rows - 1))


def split_features_target(
    df: pd.DataFrame, feature_names: list[str], target_column: str
) -> tuple[pd.DataFrame, pd.Series]:
    missing = [c for c in [*feature_names, target_column] if c not in df.columns]
    if missing:
        raise ArtifactError(
            f"training frame is missing required column(s): {missing}",
            available_columns=list(df.columns),
        )
    return df[feature_names], df[target_column]
