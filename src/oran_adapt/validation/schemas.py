"""Pydantic contract Member 4 (validation) hands to the orchestrator: V_current vs the
candidate, scored on the same held-out validation set, plus the pass/fail verdict and why."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ValidationReport(BaseModel):
    """The single gate between "a candidate model got produced" and "it may be registered"."""

    model_id: str
    metric_name: str
    current_metrics: dict[str, float] = Field(default_factory=dict)
    candidate_metrics: dict[str, float] = Field(default_factory=dict)
    current_value: float
    candidate_value: float
    threshold: float
    passed: bool
    reason: str
    n_validation_rows: int
