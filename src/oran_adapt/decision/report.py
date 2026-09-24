"""Member 2 - decision explanation: attach to every Decision (hard constraint, LLM or fallback)
the evidence it rests on, the thresholds that applied, LIVE's metrics, and what carrying it out
involves: expected cost, expected improvement, required data, resource requirement and the
fallback strategy.

Everything here is derived deterministically from the DecisionPackage and the settings, so the
same inputs always give the same explanation, and nothing is invented: an expected improvement
is only given when LIVE's degradation (or an older version's measured gain) is known, and is
None otherwise.
"""

from __future__ import annotations

from oran_adapt.analysis.schemas import DecisionPackage, VersionEvaluation
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.decision.schemas import Decision

# Preference order when the chosen strategy cannot be carried out; NO_ACTION keeps LIVE.
_FALLBACK_ORDER = [
    Strategy.FINE_TUNING,
    Strategy.FULL_RETRAINING,
    Strategy.ROLLBACK,
    Strategy.NO_ACTION,
]
_COST_LEVEL = {
    Strategy.FINE_TUNING: ("LOW", "incremental updates from LIVE's current weights"),
    Strategy.FULL_RETRAINING: ("HIGH", "a fresh fit from scratch on all training rows"),
    Strategy.ROLLBACK: ("LOW", "no training; an existing version is re-promoted"),
}
# Rough float64 working-set multiplier (features + target, a copy for the fit, the model's own).
_MEMORY_COPIES = 3


def _live(package: DecisionPackage) -> VersionEvaluation | None:
    return next((e for e in package.version_evaluations if e.is_live), None)


def _rows(package: DecisionPackage) -> tuple[int, int]:
    hist = package.historical_data.row_count if package.historical_data else 0
    drift = package.drifted_data.row_count if package.drifted_data else 0
    return hist, drift


def _fallback(strategy: Strategy, compatible: list[Strategy]) -> Strategy | None:
    if strategy not in _FALLBACK_ORDER:
        return None  # nothing was going to run, so there is nothing to fall back from
    later = _FALLBACK_ORDER[_FALLBACK_ORDER.index(strategy) + 1 :]
    return next((s for s in later if s in compatible), Strategy.NO_ACTION)


def _expected_improvement(strategy: Strategy, package: DecisionPackage) -> dict:
    live = _live(package)
    out: dict = {
        "metric": live.metric_name if live else None,
        "live_value": live.metric_value if live else None,
        "expected_gain": None,
        "basis": "LIVE was not scored on the current data; no measured basis for an estimate",
    }
    if strategy in (Strategy.FINE_TUNING, Strategy.FULL_RETRAINING):
        if live is not None and live.degradation is not None:
            out["expected_gain"] = max(0.0, live.degradation)
            out["baseline_value"] = live.baseline_metrics.get(live.metric_name or "")
            out["basis"] = (
                "LIVE's degradation from its training-time score; adapting to the drifted data "
                "is expected to recover at most that much"
            )
    elif strategy == Strategy.ROLLBACK:
        gains = (
            (package.reuse_decision.evidence.get("gains") or {}) if package.reuse_decision else {}
        )
        if gains:
            best = max(gains, key=lambda v: gains[v])
            out["expected_gain"] = gains[best]
            out["basis"] = f"version {best} measured on the current data against LIVE"
    else:
        out["expected_gain"] = 0.0
        out["basis"] = "nothing is trained or promoted"
    return out


def explain_decision(decision: Decision, package: DecisionPackage, settings: Settings) -> Decision:
    hist_rows, drift_rows = _rows(package)
    n_features = len(package.feature_shifts)
    live = _live(package)
    event = package.drift_event
    trains = decision.strategy in (Strategy.FINE_TUNING, Strategy.FULL_RETRAINING)
    level, how = _COST_LEVEL.get(decision.strategy, ("NONE", "nothing is trained or promoted"))
    train_rows = hist_rows + drift_rows if trains else 0

    evidence = {
        "task_type": package.task_type,
        "framework": package.framework,
        "model_type": package.model_type,
        "drift_score": event.drift_score,
        "severity": str(event.severity) if event.severity else None,
        "max_psi": package.max_psi,
        "min_ks_pvalue": package.min_ks_pvalue,
        "drifted_features": [
            f.feature
            for f in package.feature_shifts
            if f.psi >= settings.analysis_psi_reuse_threshold
        ],
        "historical_rows": hist_rows,
        "drifted_rows": drift_rows,
        "reuse_verdict": package.reuse_decision.verdict if package.reuse_decision else None,
        "reuse_reason": package.reuse_decision.reason if package.reuse_decision else None,
        "live_degradation": live.degradation if live else None,
        "versions_scored": len(package.version_evaluations),
        "recent_performance": package.recent_performance,
    }
    thresholds = {
        "min_drifted_rows": settings.decision_min_drifted_rows,
        "full_retrain_psi": settings.decision_full_retrain_psi_threshold,
        "drifted_feature_psi": settings.analysis_psi_reuse_threshold,
        "supported_frameworks": list(settings.decision_supported_frameworks),
        "validation_min_rows": settings.validation_min_rows,
        "validation_accuracy_tolerance": settings.validation_accuracy_tolerance,
        "validation_rmse_tolerance_ratio": settings.validation_rmse_tolerance_ratio,
    }
    return decision.model_copy(
        update={
            "evidence": evidence,
            "thresholds": thresholds,
            "metrics": dict(live.metrics)
            if live and live.metrics
            else dict(package.recent_performance),
            "expected_cost": {"level": level, "train_rows": train_rows, "basis": how},
            "expected_improvement": _expected_improvement(decision.strategy, package),
            "required_data": {
                "min_drifted_rows": settings.decision_min_drifted_rows,
                "drifted_rows_available": drift_rows,
                "historical_rows_available": hist_rows,
                "holdout_min_rows": settings.validation_min_rows,
                "labels_required": trains,
                "data_versions": [
                    ref.version
                    for ref in (package.historical_data, package.drifted_data)
                    if ref is not None
                ],
            },
            "resource_requirement": {
                "device": "cpu",
                "n_features": n_features,
                "train_rows": train_rows,
                # An estimate: rows x (features + target) x 8 bytes x working copies.
                "estimated_memory_mb": round(
                    train_rows * (n_features + 1) * 8 * _MEMORY_COPIES / 1_048_576, 3
                ),
            },
            "fallback_strategy": _fallback(decision.strategy, decision.compatible_strategies),
        }
    )
