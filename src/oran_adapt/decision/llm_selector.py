"""Member 2 - LLM strategy selection: ask the configured LLM to pick one of the hard-constraint-
approved strategies, and validate its answer before trusting it.

The LLM's opinion is advisory and disposable: any failure to reach it, parse it, or have it stay
inside the compatible set falls straight back to decision.fallback, and the reason is recorded.
Nothing downstream ever sees a strategy the constraints didn't approve. The system prompt is the
versioned ``strategy-selection`` prompt (llm.prompts).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.llm import calls
from oran_adapt.llm.client import LlmClient
from oran_adapt.llm.prompts import STRATEGY_SELECTION, Prompt, get_prompt

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger(__name__)

class LlmStrategyChoice(BaseModel):
    """The only shape an LLM answer is accepted in: exactly these three fields, strictly typed.
    Anything else - extra keys, prose, a strategy name inside a sentence - is rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    strategy: Strategy
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)


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
        "drift_summary": (
            package.drift_summary.model_dump(mode="json") if package.drift_summary else None
        ),
        "constraints": {"device": "cpu", "one_job_per_model": True},
        "compatible_strategies": [s.value for s in compatible],
    }
    return json.dumps(evidence, indent=2, default=str)


def _extract_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json")
    return json.loads(stripped)


def select_strategy_checked(
    client: LlmClient,
    package: DecisionPackage,
    compatible: list[Strategy],
    settings: Settings | None = None,
) -> tuple[LlmStrategyChoice | None, str | None, Prompt]:
    """(choice, None) when the LLM's answer can be trusted, else (None, fallback reason); never
    raises for anything the LLM does. The prompt is returned so the decision can stamp it."""
    prompt = get_prompt(STRATEGY_SELECTION, settings)
    if not compatible:
        return None, calls.FALLBACK_OUTSIDE_SET, prompt
    context = {"model_id": package.model_id, "component": "decision"}

    try:
        raw = calls.ask(client, prompt, _build_user_prompt(package, compatible), purpose="decision")
    except LlmUnavailableError as exc:
        logger.warning(
            "LLM call failed, falling back to deterministic selection",
            extra={**context, "error_code": exc.code, "reason": exc.reason},
        )
        return None, exc.reason, prompt

    try:
        # strict=True would refuse the enum from its JSON string, so validate in JSON mode.
        choice = LlmStrategyChoice.model_validate_json(json.dumps(_extract_json(raw)))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "LLM response failed validation, falling back to deterministic selection",
            extra={**context, "error": type(exc).__name__},
        )
        return None, calls.FALLBACK_INVALID_OUTPUT, prompt

    if choice.strategy not in compatible:
        logger.warning(
            "LLM chose a strategy outside the compatible set, falling back",
            extra={**context, "strategy": choice.strategy.value},
        )
        return None, calls.FALLBACK_OUTSIDE_SET, prompt

    return choice, None, prompt


def select_strategy_via_llm(
    client: LlmClient,
    package: DecisionPackage,
    compatible: list[Strategy],
    settings: Settings | None = None,
) -> LlmStrategyChoice | None:
    """The trusted choice, or None (never raises) so the caller can fall back deterministically."""
    return select_strategy_checked(client, package, compatible, settings)[0]
