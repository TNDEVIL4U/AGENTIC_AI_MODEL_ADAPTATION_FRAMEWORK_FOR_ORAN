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
