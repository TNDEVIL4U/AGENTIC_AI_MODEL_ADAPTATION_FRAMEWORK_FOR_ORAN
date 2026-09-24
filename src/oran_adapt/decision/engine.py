"""Member 2 - decision engine entry point: hard constraints -> LLM (if configured) -> fallback.

This is the single call the orchestrator (Phase 9) makes into Member 2, given the DecisionPackage
Member 1 produced. It never invents evidence and it never lets an LLM choose outside what the hard
constraints allow.
"""

from __future__ import annotations

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.config import Settings
from oran_adapt.decision.constraints import evaluate_constraints
from oran_adapt.decision.fallback import select_strategy_fallback
from oran_adapt.decision.llm_selector import select_strategy_via_llm
from oran_adapt.decision.report import explain_decision
from oran_adapt.decision.schemas import Decision
from oran_adapt.llm.client import LlmClient


def _choose(
    package: DecisionPackage, settings: Settings, llm_client: LlmClient | None
) -> Decision:
    constraint_result = evaluate_constraints(package, settings)

    if constraint_result.forced_strategy is not None:
        return Decision(
            model_id=package.model_id,
            strategy=constraint_result.forced_strategy,
            confidence=1.0,
            rationale=constraint_result.reason,
            compatible_strategies=[],
            source="HARD_CONSTRAINT",
        )

    compatible = constraint_result.compatible_strategies

    if llm_client is not None:
        llm_choice = select_strategy_via_llm(llm_client, package, compatible)
        if llm_choice is not None:
            return Decision(
                model_id=package.model_id,
                strategy=llm_choice.strategy,
                confidence=llm_choice.confidence,
                rationale=llm_choice.rationale,
                compatible_strategies=compatible,
                source="LLM",
            )

    strategy, reason = select_strategy_fallback(compatible)
    return Decision(
        model_id=package.model_id,
        strategy=strategy,
        confidence=0.5,
        rationale=reason,
        compatible_strategies=compatible,
        source="FALLBACK",
    )


def decide(
    package: DecisionPackage, settings: Settings, llm_client: LlmClient | None
) -> Decision:
    """Choose a strategy and explain it: rationale and confidence come from whichever source
    chose; evidence, thresholds, metrics, expected cost and improvement, required data,
    resource requirement and fallback strategy are always computed deterministically."""
    return explain_decision(_choose(package, settings, llm_client), package, settings)
