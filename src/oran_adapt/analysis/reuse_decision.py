"""Member 1 - reuse decision: given every version's score on the current data, either pick an
existing version to go live (no training at all) or hand over to Member 2 with a hint.

Deterministic rule:

* Only compatible, non-live versions are eligible, and only if no older than
  ``reuse_max_model_age_days`` (when set).
* A classifier version must beat LIVE's accuracy by at least ``reuse_min_accuracy_gain``
  (absolute); a regressor version must cut LIVE's RMSE by at least
  ``reuse_min_rmse_reduction_ratio`` of LIVE's RMSE.
* Of the eligible versions, the best score wins; a tie goes to the newest version.
* With no winner the verdict is RETRAIN_MODEL when ``max_psi`` reaches
  ``decision_full_retrain_psi_threshold``, otherwise ADAPT_MODEL. That is a hint only.
"""

from __future__ import annotations

from oran_adapt.analysis.schemas import ReuseDecision, VersionEvaluation
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import ReuseVerdict


def _hint(max_psi: float, settings: Settings) -> ReuseVerdict:
    if max_psi >= settings.decision_full_retrain_psi_threshold:
        return ReuseVerdict.RETRAIN_MODEL
    return ReuseVerdict.ADAPT_MODEL


def decide_reuse(
    evaluations: list[VersionEvaluation],
    *,
    live_version: str | None,
    max_psi: float,
    settings: Settings,
) -> ReuseDecision:
    live = next((e for e in evaluations if e.is_live), None)
    fallback = _hint(max_psi, settings)
    if live is None or not live.compatible or live.metric_value is None:
        why = "LIVE could not be scored on the current data"
        if live is not None and live.incompatibility_reason:
            why += f" ({live.incompatibility_reason})"
        return ReuseDecision(verdict=fallback.value, live_version=live_version, reason=why)

    metric = live.metric_name
    higher_better = metric == "accuracy"
    confidence = min(1.0, live.n_rows / settings.reuse_confidence_rows)

    def gain(ev: VersionEvaluation) -> float:
        assert ev.metric_value is not None and live.metric_value is not None
        return ev.metric_value - live.metric_value if higher_better else live.metric_value - ev.metric_value

    required = (
        settings.reuse_min_accuracy_gain
        if higher_better
        else live.metric_value * settings.reuse_min_rmse_reduction_ratio
    )
    max_age = settings.reuse_max_model_age_days
    eligible = [
        ev
        for ev in evaluations
        if not ev.is_live
        and ev.compatible
        and ev.metric_name == metric
        and ev.metric_value is not None
        and (max_age is None or ev.age_days is None or ev.age_days <= max_age)
        and gain(ev) >= required
    ]

    if not eligible:
        compatible = [e for e in evaluations if e.compatible and not e.is_live]
        return ReuseDecision(
            verdict=fallback.value,
            live_version=live_version,
            metric_name=metric,
            live_value=live.metric_value,
            confidence=confidence,
            reason=(
                f"no registered version beats LIVE v{live_version} ({metric}="
                f"{live.metric_value:.4f}) by the required {required:.4f}; "
                f"{len(compatible)} other compatible version(s) scored"
            ),
        )

    best = max(eligible, key=lambda ev: (gain(ev), int(ev.version)))
    return ReuseDecision(
        verdict=ReuseVerdict.REUSE_EXISTING_VERSION.value,
        live_version=live_version,
        selected_version=best.version,
        metric_name=metric,
        live_value=live.metric_value,
        selected_value=best.metric_value,
        improvement=gain(best),
        confidence=confidence,
        reason=(
            f"version {best.version} scores {metric}={best.metric_value:.4f} on the current "
            f"data vs LIVE v{live_version} {live.metric_value:.4f} (gain {gain(best):.4f} >= "
            f"{required:.4f}); reusing it, no training needed"
        ),
    )
