"""Pydantic contracts Member 1 hands to the orchestrator and to Member 2 (decision).

Kept separate from core.schemas, which is reserved for the O-RAN-facing API contracts;
these are internal pipeline contracts between analysis and decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from oran_adapt.core.schemas import DriftEvent


class FeatureShift(BaseModel):
    feature: str
    historical_mean: float
    drifted_mean: float
    historical_std: float
    drifted_std: float
    ks_statistic: float
    ks_pvalue: float
    psi: float


class DataVersionRef(BaseModel):
    data_version_id: int
    version: str
    kind: str
    row_count: int
    data_start: datetime | None = None
    data_end: datetime | None = None


class DecisionPackage(BaseModel):
    """Everything Member 2 needs to choose a strategy, without re-deriving it."""

    model_id: str
    model_type: str | None = None
    framework: str | None = None
    drift_event: DriftEvent
    reuse_reason: str
    historical_data: DataVersionRef | None = None
    drifted_data: DataVersionRef | None = None
    feature_shifts: list[FeatureShift] = Field(default_factory=list)
    max_psi: float = 0.0
    min_ks_pvalue: float = 1.0
    merge_overlap_count: int = 0
    recent_performance: dict[str, float] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    status: Literal["REUSE", "PACKAGED", "INSUFFICIENT_DATA"]
    model_id: str
    reuse: bool
    reason: str
    decision_package: DecisionPackage | None = None
