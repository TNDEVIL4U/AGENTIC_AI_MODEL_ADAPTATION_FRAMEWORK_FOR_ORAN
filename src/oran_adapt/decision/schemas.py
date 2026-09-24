"""Pydantic contracts Member 2 hands to the orchestrator (and, later, to Member 3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oran_adapt.core.enums import Strategy


class Decision(BaseModel):
    model_id: str
    strategy: Strategy
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    compatible_strategies: list[Strategy] = Field(default_factory=list)
    source: Literal["HARD_CONSTRAINT", "LLM", "FALLBACK"]
    # Filled in deterministically by decision.report for every source, so an LLM-made choice
    # is explained by the same measured evidence as a rule-made one.
    evidence: dict = Field(default_factory=dict)
    thresholds: dict = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    expected_cost: dict = Field(default_factory=dict)
    expected_improvement: dict = Field(default_factory=dict)
    required_data: dict = Field(default_factory=dict)
    resource_requirement: dict = Field(default_factory=dict)
    # What to do if this strategy cannot be carried out; NO_ACTION keeps LIVE as it is.
    fallback_strategy: Strategy | None = None
