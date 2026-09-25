"""Member 2 - decision engine entry point: hard constraints -> LLM (if configured) -> fallback.

This is the single call the orchestrator (Phase 9) makes into Member 2, given the DecisionPackage
Member 1 produced. It never invents evidence and it never lets an LLM choose outside what the hard
constraints allow.
"""

from __future__ import annotations

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.decision.constraints import ConstraintResult, evaluate_constraints
from oran_adapt.decision.fallback import select_strategy_fallback
from oran_adapt.decision.llm_selector import select_strategy_via_llm
from oran_adapt.decision.report import explain_decision
from oran_adapt.decision.schemas import Decision
from oran_adapt.llm.client import LlmClient


def _with_rejections(decision: Decision, ruled_out: dict[Strategy, str]) -> Decision:
    rejected = dict(ruled_out)
    for strategy in decision.compatible_strategies:
        if strategy != decision.strategy:
            rejected[strategy] = (
                f"compatible, but the {decision.source.lower()} selection preferred "
                f"{decision.strategy}"
            )
    rejected.pop(decision.strategy, None)
    return decision.model_copy(update={"rejected_strategies": rejected})


def _evidence_confidence(package: DecisionPackage, settings: Settings) -> float | None:
    """How far the evidence supports acting on the drift, in (0, 1]: the drifted sample size
    against the rows that count as full confidence, scaled down when no feature is significant
    after the multiple-testing correction (more so when none is even affected). None for a
    package without a drift summary, which keeps the older fixed confidences."""
    summary = package.drift_summary
    if summary is None:
        return None
    sample = min(1.0, summary.drifted_rows / settings.reuse_confidence_rows)
    if summary.significant_features:
        significance = 1.0
    elif summary.affected_features:
        significance = 0.7
    else:
        significance = 0.4
    return max(0.01, round(sample * significance, 4))


def _choose(package: DecisionPackage, settings: Settings, llm_client: LlmClient | None) -> Decision:
    constraint_result = evaluate_constraints(package, settings)
    return _with_rejections(
        _select(package, constraint_result, llm_client, _evidence_confidence(package, settings)),
        constraint_result.rejected,
    )


def _select(
    package: DecisionPackage,
    constraint_result: ConstraintResult,
    llm_client: LlmClient | None,
    evidence_confidence: float | None,
) -> Decision:

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
            # The LLM cannot be surer than the evidence allows; its own figure is kept.
            confidence = llm_choice.confidence
            if evidence_confidence is not None:
                confidence = min(confidence, evidence_confidence)
            return Decision(
                model_id=package.model_id,
                strategy=llm_choice.strategy,
                confidence=confidence,
                rationale=llm_choice.rationale,
                compatible_strategies=compatible,
                source="LLM",
                evidence={"llm_confidence": llm_choice.confidence},
            )

    strategy, reason = select_strategy_fallback(compatible)
    return Decision(
        model_id=package.model_id,
        strategy=strategy,
        confidence=0.5 if evidence_confidence is None else evidence_confidence,
        rationale=reason,
        compatible_strategies=compatible,
        source="FALLBACK",
    )


def decide(package: DecisionPackage, settings: Settings, llm_client: LlmClient | None) -> Decision:
    """Choose a strategy and explain it: rationale and confidence come from whichever source
    chose; evidence, thresholds, metrics, expected cost and improvement, required data,
    resource requirement and fallback strategy are always computed deterministically."""
    return explain_decision(_choose(package, settings, llm_client), package, settings)
