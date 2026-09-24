"""Member 2 - LLM strategy selection: ask the configured LLM to pick one of the hard-constraint-
approved strategies, and validate its answer before trusting it.

The LLM's opinion is advisory and disposable: any failure to reach it, parse it, or have it stay
inside the compatible set falls straight back to decision.fallback. Nothing downstream ever sees
a strategy the constraints didn't approve.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.llm.client import LlmClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the strategy-selection component of an O-RAN model adaptation pipeline. "
    "You are given evidence that a deployed model has drifted and a closed list of strategies "
    "that are technically compatible with this situation. Choose exactly one strategy from that "
    "list and justify it briefly using the evidence given. "
    "Respond with ONLY a single JSON object, no markdown fences, no prose outside the JSON, "
    'matching this shape: {"strategy": "<one of the compatible strategies>", '
    '"confidence": <float 0..1>, "rationale": "<one or two sentences>"}.'
)


class LlmStrategyChoice(BaseModel):
    strategy: Strategy
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


def _build_user_prompt(package: DecisionPackage, compatible: list[Strategy]) -> str:
    shifts = [
        {
            "feature": f.feature,
            "historical_mean": f.historical_mean,
            "drifted_mean": f.drifted_mean,
            "psi": f.psi,
            "ks_pvalue": f.ks_pvalue,
        }
        for f in package.feature_shifts
    ]
    evidence = {
        "model_id": package.model_id,
        "model_type": package.model_type,
        "framework": package.framework,
        "task_type": package.task_type,
        "reuse_reason": package.reuse_reason,
        "drift_score": package.drift_event.drift_score,
        "severity": package.drift_event.severity,
        "max_psi": package.max_psi,
        "min_ks_pvalue": package.min_ks_pvalue,
        "feature_shifts": shifts,
        "recent_performance": package.recent_performance,
        "historical_rows": package.historical_data.row_count if package.historical_data else 0,
        "drifted_rows": package.drifted_data.row_count if package.drifted_data else 0,
        # How each registered version scored on the current data, and Member 1's verdict.
        "version_evaluations": [
            {
                "version": e.version,
                "is_live": e.is_live,
                "compatible": e.compatible,
                "metrics": e.metrics,
                "degradation": e.degradation,
                "age_days": e.age_days,
            }
            for e in package.version_evaluations
        ],
        "reuse_verdict": package.reuse_decision.verdict if package.reuse_decision else None,
        "constraints": {"device": "cpu", "one_job_per_model": True},
        "compatible_strategies": [s.value for s in compatible],
    }
    return json.dumps(evidence, indent=2, default=str)


def _extract_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json")
    return json.loads(stripped)


def select_strategy_via_llm(
    client: LlmClient, package: DecisionPackage, compatible: list[Strategy]
) -> LlmStrategyChoice | None:
    """Returns None (never raises) whenever the LLM's answer can't be trusted, so the caller can
    fall back deterministically."""
    if not compatible:
        return None

    try:
        raw = client.complete(system=_SYSTEM_PROMPT, prompt=_build_user_prompt(package, compatible))
    except LlmUnavailableError as exc:
        logger.warning(
            "LLM call failed, falling back to deterministic selection",
            extra={"model_id": package.model_id, "component": "decision", "error": str(exc)},
        )
        return None

    try:
        choice = LlmStrategyChoice.model_validate(_extract_json(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "LLM response failed validation, falling back to deterministic selection",
            extra={
                "model_id": package.model_id,
                "component": "decision",
                "error": str(exc),
            },
        )
        return None

    if choice.strategy not in compatible:
        logger.warning(
            "LLM chose a strategy outside the compatible set, falling back",
            extra={
                "model_id": package.model_id,
                "component": "decision",
                "strategy": choice.strategy.value,
            },
        )
        return None

    return choice
