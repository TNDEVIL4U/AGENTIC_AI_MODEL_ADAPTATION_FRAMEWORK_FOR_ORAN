"""Member 3 - data leakage checks: run on the training rows after the held-out rows (CurrentData)
are chosen and before any engine fits, so a candidate is never scored on rows it has seen.

* train/test leakage: a held-out row, or a copy of one (same feature and target values), is
  dropped from the training rows.
* temporal leakage / future contamination: the hold-out is the newest rows, so a training row
  observed after the oldest held-out row is dropped (unless leakage_allow_future_rows says the
  data is not a time series). Splits are temporal; nothing is shuffled.
* target leakage: the target listed as a feature, or a feature with the target's exact values
  (or, when leakage_target_correlation_max is set, that correlated with it) fails the job with
  DataLeakageError, since dropping rows cannot fix it.
* preprocessing leakage: preprocessing lives inside the model (a fitted pipeline) and engines fit
  only on the rows returned here, so it never sees held-out rows; the report records that the
  two sets are disjoint.
* duplicate rows inside the training set are counted (not removed; CurrentData cleaning already
  resolved duplicate record keys).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from oran_adapt.core.config import Settings
from oran_adapt.core.errors import DataLeakageError
from oran_adapt.db.models import DataRecord


class LeakageReport(BaseModel):
    checked: bool = True
    split: str = "temporal"
    train_rows_in: int = 0
    train_rows_out: int = 0
    holdout_rows: int = 0
    holdout_overlap_removed: int = 0
    holdout_duplicates_removed: int = 0
    future_rows_removed: int = 0
    train_duplicate_rows: int = 0
    holdout_start: str | None = None
    train_end: str | None = None
    train_holdout_disjoint: bool = True
    preprocessing_fit_on: str = "training rows only"
    target_leakage: list[str] = Field(default_factory=list)


def _row_key(payload: dict, columns: list[str]) -> str:
    return json.dumps({c: payload.get(c) for c in columns}, sort_keys=True, default=str)


def _target_leaks(
    frame: pd.DataFrame, target: str, features: list[str], corr_max: float | None
) -> list[str]:
    leaks = [f"{target} (the target is a feature)"] if target in features else []
    if target not in frame.columns or frame[target].nunique(dropna=True) < 2:
        return leaks
    y = frame[target]
    for feature in features:
        if feature == target or feature not in frame.columns:
            continue
        x = frame[feature]
        both = x.notna() & y.notna()
        if both.sum() < 2:
            continue
        xs, ys = x[both], y[both]
        if np.array_equal(xs.to_numpy(), ys.to_numpy()):
            leaks.append(f"{feature} (identical to the target)")
            continue
        if corr_max is None:
            continue
        xn, yn = pd.to_numeric(xs, errors="coerce"), pd.to_numeric(ys, errors="coerce")
        if xn.isna().any() or yn.isna().any() or xn.nunique() < 2:
            continue
        corr = abs(float(np.corrcoef(xn, yn)[0, 1]))
        if corr >= corr_max:
            leaks.append(f"{feature} (|corr| {corr:.4f} with the target)")
    return leaks


def check_leakage(
    train: list[DataRecord],
    holdout: list[DataRecord],
    *,
    target: str,
    feature_names: list[str],
    settings: Settings,
) -> tuple[list[DataRecord], LeakageReport]:
    """Return the training rows that are safe to fit on, and what was found. Raises
    DataLeakageError on target leakage, or if no training rows survive the checks."""
    report = LeakageReport(train_rows_in=len(train), holdout_rows=len(holdout))
    if not settings.leakage_checks_enabled:
        report.checked = False
        report.train_rows_out = len(train)
        return train, report

    columns = sorted({*feature_names, target})
    holdout_ids = {r.id for r in holdout}
    holdout_keys = {_row_key(r.payload, columns) for r in holdout}

    kept = [r for r in train if r.id not in holdout_ids]
    report.holdout_overlap_removed = len(train) - len(kept)

    before = len(kept)
    kept = [r for r in kept if _row_key(r.payload, columns) not in holdout_keys]
    report.holdout_duplicates_removed = before - len(kept)

    if holdout:
        cutoff = min(r.observed_at for r in holdout)
        report.holdout_start = cutoff.isoformat()
        if not settings.leakage_allow_future_rows:
            before = len(kept)
            kept = [r for r in kept if r.observed_at <= cutoff]
            report.future_rows_removed = before - len(kept)
        else:
            report.split = "unordered (future rows allowed by configuration)"

    if kept:
        report.train_end = max(r.observed_at for r in kept).isoformat()
    keys = [_row_key(r.payload, columns) for r in kept]
    report.train_duplicate_rows = len(keys) - len(set(keys))
    report.train_holdout_disjoint = not ({r.id for r in kept} & holdout_ids)
    report.train_rows_out = len(kept)

    frame = pd.DataFrame([r.payload for r in kept])
    report.target_leakage = _target_leaks(
        frame, target, feature_names, settings.leakage_target_correlation_max
    )
    if report.target_leakage:
        raise DataLeakageError(
            f"target leakage: {', '.join(report.target_leakage)}",
            report=report.model_dump(),
        )
    if not kept:
        raise DataLeakageError(
            "no training rows left after the leakage checks", report=report.model_dump()
        )
    return kept, report
