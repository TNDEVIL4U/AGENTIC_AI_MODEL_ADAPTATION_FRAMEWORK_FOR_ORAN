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


class VersionEvaluation(BaseModel):
    """How one registered version scored on the current (held-out, newest drifted) data."""

    version: str
    is_live: bool = False
    compatible: bool = False
    incompatibility_reason: str | None = None
    estimator_type: str | None = None
    metric_name: str | None = None  # the task's primary metric (validation.metrics)
    metric_value: float | None = None
    # Every metric of the task that is defined on the current data, primary first.
    metrics: dict[str, float] = Field(default_factory=dict)
    # Metrics logged on the version's source run when it was trained, and how far the current
    # score has fallen from that (positive = worse now), in the metric's own units.
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    degradation: float | None = None
    age_days: float | None = None
    n_rows: int = 0
    artifact_sha256: str | None = None


class ReuseDecision(BaseModel):
    """Member 1's deterministic verdict after scoring every version. REUSE_EXISTING_VERSION is
    final; ADAPT_MODEL / RETRAIN_MODEL are only hints for Member 2, which stays the authority on
    how to adapt."""

    verdict: str  # ReuseVerdict
    live_version: str | None = None
    selected_version: str | None = None
    metric_name: str | None = None
    live_value: float | None = None
    selected_value: float | None = None
    improvement: float | None = None
    # min(1, scored rows / reuse_confidence_rows): a heuristic for how much evidence the
    # comparison rests on, not a statistical confidence level.
    confidence: float = 0.0
    reason: str
    # What the verdict rests on: the drift and LIVE-degradation evidence, the thresholds that
    # were applied, and every scored version's primary metric ({version: value}).
    evidence: dict = Field(default_factory=dict)
    thresholds: dict = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)


class DecisionPackage(BaseModel):
    """Everything Member 2 needs to choose a strategy, without re-deriving it."""

    model_id: str
    model_type: str | None = None
    framework: str | None = None
    task_type: str | None = None
    drift_event: DriftEvent
    reuse_reason: str
    historical_data: DataVersionRef | None = None
    drifted_data: DataVersionRef | None = None
    feature_shifts: list[FeatureShift] = Field(default_factory=list)
    max_psi: float = 0.0
    min_ks_pvalue: float = 1.0
    merge_overlap_count: int = 0
    recent_performance: dict[str, float] = Field(default_factory=dict)
    # Filled in by the orchestrator after Member 1 scored the registered versions.
    version_evaluations: list[VersionEvaluation] = Field(default_factory=list)
    reuse_decision: ReuseDecision | None = None


class AnalysisResult(BaseModel):
    status: Literal["REUSE", "PACKAGED", "INSUFFICIENT_DATA"]
    model_id: str
    reuse: bool
    reason: str
    decision_package: DecisionPackage | None = None
