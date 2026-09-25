"""Member 2 - hard constraints: the rules no LLM gets to overrule.

These run before any LLM call. They either narrow the field to the strategies that are actually
executable given what Member 1 observed, or short-circuit the decision entirely (no compatible
strategy exists, or there isn't enough evidence to choose one at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy


@dataclass
class ConstraintResult:
    compatible_strategies: list[Strategy] = field(default_factory=list)
    reason: str = ""
    forced_strategy: Strategy | None = None
    # Each adaptation strategy the constraints ruled out, and why.
    rejected: dict[Strategy, str] = field(default_factory=dict)


_CANDIDATES = (
    Strategy.FULL_RETRAINING,
    Strategy.FINE_TUNING,
    Strategy.ROLLBACK,
    Strategy.NO_ACTION,
)


def _forced(strategy: Strategy, reason: str) -> ConstraintResult:
    return ConstraintResult(
        forced_strategy=strategy, reason=reason, rejected=dict.fromkeys(_CANDIDATES, reason)
    )


def evaluate_constraints(package: DecisionPackage, settings: Settings) -> ConstraintResult:
    supported = {f.lower() for f in settings.decision_supported_frameworks}
    if package.framework is None or package.framework.lower() not in supported:
        return _forced(
            Strategy.NO_COMPATIBLE_STRATEGY,
            f"framework {package.framework!r} has no adaptation engine "
            f"(supported: {sorted(supported)})",
        )

    drifted_rows = package.drifted_data.row_count if package.drifted_data else 0
    if drifted_rows < settings.decision_min_drifted_rows:
        return _forced(
            Strategy.INSUFFICIENT_INFORMATION,
            f"only {drifted_rows} drifted rows available, "
            f"need >= {settings.decision_min_drifted_rows}",
        )

    if not package.feature_shifts:
        return _forced(
            Strategy.INSUFFICIENT_INFORMATION,
            "no comparable feature statistics to decide a strategy from",
        )

    summary = package.drift_summary
    if summary is not None and not summary.sample_sufficient:
        return _forced(Strategy.INSUFFICIENT_INFORMATION, summary.sample_note)

    compatible = list(_CANDIDATES)
    notes = []
    rejected: dict[Strategy, str] = {}

    def _rule_out(strategy: Strategy, why: str) -> None:
        if strategy in compatible:
            compatible.remove(strategy)
            notes.append(why)
            rejected[strategy] = why

    if package.max_psi >= settings.decision_full_retrain_psi_threshold:
        _rule_out(
            Strategy.FINE_TUNING,
            f"max_psi {package.max_psi:.3f} >= "
            f"{settings.decision_full_retrain_psi_threshold} rules out fine-tuning",
        )

    # Framework capabilities: without a native engine, only the LLM adapter can carry out
    # that kind of training.
    caps = summary.capabilities if summary is not None else {}
    llm_adapter = caps.get("llm_adapter", False)
    if caps and not caps.get("fine_tuning", False) and not llm_adapter:
        _rule_out(
            Strategy.FINE_TUNING,
            f"{package.framework} has no fine-tuning engine and no LLM adapter is configured",
        )
    if caps and not caps.get("full_retraining", False) and not llm_adapter:
        _rule_out(
            Strategy.FULL_RETRAINING,
            f"{package.framework} has no retraining engine and no LLM adapter is configured",
        )

    if not package.recent_performance:
        _rule_out(Strategy.ROLLBACK, "no recent performance data to justify a rollback")
    elif package.version_evaluations and not any(
        e.compatible and not e.is_live for e in package.version_evaluations
    ):
        _rule_out(Strategy.ROLLBACK, "no compatible earlier version to roll back to")

    reason = "; ".join(notes) if notes else "no additional hard constraints triggered"
    return ConstraintResult(compatible_strategies=compatible, reason=reason, rejected=rejected)
