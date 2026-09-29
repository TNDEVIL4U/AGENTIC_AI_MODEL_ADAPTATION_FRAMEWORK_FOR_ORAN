"""Member 3 - training data: pull the real feature/target rows a data version points at, so
retraining and fine-tuning engines fit on the same data Member 1 analyzed - never synthetic or
re-derived data. Rows come through datastore.access, so a version stored by reference is read
from its object the same way a database-stored one is read from data_record."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

import pandas as pd

from oran_adapt.core.errors import ArtifactError
from oran_adapt.datastore.access import DataAccess, Row

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def load_data_version_frame(
    session: Session, data_version_id: int, *, access: DataAccess | None = None
) -> pd.DataFrame:
    """One data version's rows, wherever they are stored (within DATASET_MAX_ROWS)."""
    return (access or DataAccess()).frame(session, data_version_id)


def build_training_frame(
    session: Session, data_version_ids: list[int], *, access: DataAccess | None = None
) -> pd.DataFrame:
    frames = [load_data_version_frame(session, i, access=access) for i in data_version_ids]
    return pd.concat(frames, ignore_index=True)


def load_records(
    session: Session, data_version_ids: list[int], *, access: DataAccess | None = None
) -> list[Row]:
    """All rows of the given data versions, oldest first (id breaks timestamp ties). Refused
    with DataTooLargeError, before reading, past DATASET_MAX_ROWS."""
    return (access or DataAccess()).load_records(session, data_version_ids)


def records_frame(records: Sequence[Row]) -> pd.DataFrame:
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
