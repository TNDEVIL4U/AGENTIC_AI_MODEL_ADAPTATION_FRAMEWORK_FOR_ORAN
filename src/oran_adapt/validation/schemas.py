"""Pydantic contract Member 4 (validation) hands to the orchestrator: V_current vs the
candidate, scored on the same held-out validation set, plus the gate's structured decision."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GuardrailResult(BaseModel):
    """One guardrail's outcome. ``evaluated`` False: it did not apply (e.g. calibration on a
    regressor) or its input was missing; ``passed`` is then True unless the policy requires it."""

    name: str
    passed: bool
    evaluated: bool
    detail: str
    values: dict[str, Any] = Field(default_factory=dict)


class GateDecision(BaseModel):
    """What the gate decided and exactly why (validation.gate), stored as a gate_decision row.

    ``delta`` is the candidate's improvement on the primary metric, oriented so that positive
    is better; ``ci_low``/``ci_high`` its paired-bootstrap interval at ``confidence``. The
    primary test passes when ``ci_low > threshold``."""

    verdict: Literal["ACCEPT", "REJECT"]
    reasons: list[str]
    policy_version: str
    policy_hash: str
    mode: str
    test: str
    metric: str
    higher_is_better: bool
    current_value: float
    candidate_value: float
    delta: float
    ci_low: float
    ci_high: float
    confidence: float
    threshold: float
    resamples: int
    n_rows: int
    guardrails: list[GuardrailResult] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """The single gate between "a candidate model got produced" and "it may be registered"."""

    model_id: str
    metric_name: str
    current_metrics: dict[str, float] = Field(default_factory=dict)
    candidate_metrics: dict[str, float] = Field(default_factory=dict)
    current_value: float
    candidate_value: float
    # The primary test's threshold on the oriented improvement (GateDecision.threshold).
    threshold: float
    passed: bool
    reason: str
    n_validation_rows: int
    gate: GateDecision | None = None
