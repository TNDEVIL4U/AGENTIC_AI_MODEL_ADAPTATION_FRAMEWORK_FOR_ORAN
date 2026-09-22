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


def evaluate_constraints(package: DecisionPackage, settings: Settings) -> ConstraintResult:
    supported = {f.lower() for f in settings.decision_supported_frameworks}
    if package.framework is None or package.framework.lower() not in supported:
        return ConstraintResult(
            forced_strategy=Strategy.NO_COMPATIBLE_STRATEGY,
            reason=(
                f"framework {package.framework!r} has no adaptation engine "
                f"(supported: {sorted(supported)})"
            ),
        )

    drifted_rows = package.drifted_data.row_count if package.drifted_data else 0
    if drifted_rows < settings.decision_min_drifted_rows:
        return ConstraintResult(
            forced_strategy=Strategy.INSUFFICIENT_INFORMATION,
            reason=(
                f"only {drifted_rows} drifted rows available, "
                f"need >= {settings.decision_min_drifted_rows}"
            ),
        )

    if not package.feature_shifts:
        return ConstraintResult(
            forced_strategy=Strategy.INSUFFICIENT_INFORMATION,
            reason="no comparable feature statistics to decide a strategy from",
        )

    compatible = [
        Strategy.FULL_RETRAINING,
        Strategy.FINE_TUNING,
        Strategy.ROLLBACK,
        Strategy.NO_ACTION,
    ]
    notes = []

    if package.max_psi >= settings.decision_full_retrain_psi_threshold:
        compatible.remove(Strategy.FINE_TUNING)
        notes.append(
            f"max_psi {package.max_psi:.3f} >= "
            f"{settings.decision_full_retrain_psi_threshold} rules out fine-tuning"
        )

    if not package.recent_performance:
        compatible.remove(Strategy.ROLLBACK)
        notes.append("no recent performance data to justify a rollback")

    reason = "; ".join(notes) if notes else "no additional hard constraints triggered"
    return ConstraintResult(compatible_strategies=compatible, reason=reason)
