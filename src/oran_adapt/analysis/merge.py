"""Member 1 - timestamp merge: align the historical and drifted slices into one time series.

The merge is what lets comparison reason about "before" and "after" honestly: it establishes
the boundary (last historical observation) and flags drifted rows that actually land at or
before that boundary, which signals late-arriving or clock-skewed data rather than genuine
post-boundary drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from oran_adapt.analysis.retrieval import DataSlice


@dataclass
class MergedSeries:
    rows: list[dict]  # {"observed_at": datetime, "segment": "historical"|"drifted", **features}
    historical_count: int
    drifted_count: int
    boundary_at: datetime | None  # last historical timestamp; None if there is no historical data
    overlap_count: int  # drifted rows observed at or before boundary_at


def timestamp_merge(historical: DataSlice | None, drifted: DataSlice | None) -> MergedSeries:
    rows: list[dict] = []
    boundary_at: datetime | None = None

    if historical is not None and historical.records:
        boundary_at = max(r["observed_at"] for r in historical.records)
        rows.extend({"segment": "historical", **r} for r in historical.records)

    overlap_count = 0
    if drifted is not None:
        for r in drifted.records:
            if boundary_at is not None and r["observed_at"] <= boundary_at:
                overlap_count += 1
            rows.append({"segment": "drifted", **r})

    rows.sort(key=lambda r: r["observed_at"])

    return MergedSeries(
        rows=rows,
        historical_count=historical.row_count if historical else 0,
        drifted_count=drifted.row_count if drifted else 0,
        boundary_at=boundary_at,
        overlap_count=overlap_count,
    )
