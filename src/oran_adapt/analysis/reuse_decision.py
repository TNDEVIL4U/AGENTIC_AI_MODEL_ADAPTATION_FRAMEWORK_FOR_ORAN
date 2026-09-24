"""Member 1 - reuse decision: given every version's score on the current data, either pick an
existing version to go live (no training at all) or hand over to Member 2 with a hint.

Deterministic rule:

* Only compatible, non-live versions are eligible, and only if no older than
  ``reuse_max_model_age_days`` (when set).
* Versions are compared on the task's primary metric (validation.metrics). For a
  higher-is-better metric (accuracy, F1, silhouette) a version must beat LIVE by at least
  ``reuse_min_accuracy_gain`` (absolute); for an error metric (RMSE) it must cut LIVE's value
  by at least ``reuse_min_rmse_reduction_ratio`` of it.
* Of the eligible versions, the best score wins; a tie goes to the newest version.
* With no winner the verdict is RETRAIN_MODEL when ``max_psi`` reaches
  ``decision_full_retrain_psi_threshold``, otherwise ADAPT_MODEL. That is a hint only.
"""

from __future__ import annotations

from oran_adapt.analysis.schemas import ReuseDecision, VersionEvaluation
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import ReuseVerdict
from oran_adapt.validation.metrics import higher_is_better


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
    thresholds: dict = {
        "min_gain_higher_is_better": settings.reuse_min_accuracy_gain,
        "min_error_reduction_ratio": settings.reuse_min_rmse_reduction_ratio,
        "max_model_age_days": settings.reuse_max_model_age_days,
        "confidence_rows": settings.reuse_confidence_rows,
        "full_retrain_psi": settings.decision_full_retrain_psi_threshold,
    }
    scored = {e.version: e.metric_value for e in evaluations if e.metric_value is not None}
    evidence: dict = {
        "max_psi": max_psi,
        "versions_scored": len(evaluations),
        "versions_compatible": sum(e.compatible for e in evaluations),
        "incompatible": {
            e.version: e.incompatibility_reason for e in evaluations if not e.compatible
        },
        "live_degradation": live.degradation if live else None,
        "live_baseline_metrics": live.baseline_metrics if live else {},
    }
    explain = {"evidence": evidence, "thresholds": thresholds, "metrics": scored}
    if live is None or not live.compatible or live.metric_value is None:
        why = "LIVE could not be scored on the current data"
        if live is not None and live.incompatibility_reason:
            why += f" ({live.incompatibility_reason})"
        return ReuseDecision(
            verdict=fallback.value, live_version=live_version, reason=why, **explain
        )

    metric = live.metric_name
    assert metric is not None
    higher_better = higher_is_better(metric)
    confidence = min(1.0, live.n_rows / settings.reuse_confidence_rows)

    def gain(ev: VersionEvaluation) -> float:
        assert ev.metric_value is not None and live.metric_value is not None
        return ev.metric_value - live.metric_value if higher_better else live.metric_value - ev.metric_value

    required = (
        settings.reuse_min_accuracy_gain
        if higher_better
        else live.metric_value * settings.reuse_min_rmse_reduction_ratio
    )
    thresholds.update(metric=metric, higher_is_better=higher_better, required_gain=required)
    evidence["gains"] = {
        e.version: gain(e)
        for e in evaluations
        if not e.is_live and e.compatible and e.metric_name == metric and e.metric_value is not None
    }
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
            **explain,
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
        **explain,
    )
