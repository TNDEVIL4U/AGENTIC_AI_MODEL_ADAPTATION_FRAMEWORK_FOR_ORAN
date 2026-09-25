"""Member 1 - drift summary: the measured facts Member 2 judges a drift by, gathered once here
so the decision engine never re-derives them: how big the drift is, which features and how many
moved, on how many rows, how significant it is after correcting for testing many features at
once, how the model has been doing, which version is LIVE, and what adapting it could involve.
"""

from __future__ import annotations

from oran_adapt.analysis.comparison import ComparisonResult
from oran_adapt.analysis.merge import MergedSeries
from oran_adapt.analysis.reuse import is_shifted
from oran_adapt.analysis.schemas import DriftSummary
from oran_adapt.core.config import Settings

# Frameworks with a built-in engine for each kind of training (adaptation.engines.select_engine).
# Fine-tuning also needs the artifact itself to support it (e.g. an sklearn estimator with
# partial_fit), which is checked on the loaded model at adaptation time.
FINE_TUNE_FRAMEWORKS = frozenset({"sklearn", "torch", "pytorch"})
FULL_RETRAIN_FRAMEWORKS = frozenset({"sklearn", "xgboost", "torch", "pytorch"})


def framework_capabilities(framework: str | None, settings: Settings) -> dict[str, bool]:
    """What adapting a model of this framework could use: a built-in fine-tuning engine, a
    built-in full-retraining engine, and the LLM adapter (needs a configured provider)."""
    fw = (framework or "").lower()
    supported = {f.lower() for f in settings.decision_supported_frameworks}
    return {
        "fine_tuning": fw in supported and fw in FINE_TUNE_FRAMEWORKS,
        "full_retraining": fw in supported and fw in FULL_RETRAIN_FRAMEWORKS,
        "llm_adapter": settings.llm_provider != "none",
    }


def summarize_drift(
    comparison: ComparisonResult,
    merged: MergedSeries,
    *,
    settings: Settings,
    framework: str | None,
    recent_performance: dict[str, float],
    previous_version: str | None,
) -> DriftSummary:
    features = comparison.features
    affected = [
        f.feature
        for f in features
        if is_shifted(
            f,
            psi_threshold=settings.analysis_psi_reuse_threshold,
            ks_pvalue_threshold=settings.analysis_ks_pvalue_reuse_threshold,
            min_psi_rows=settings.analysis_min_psi_rows,
        )
    ]
    # Bonferroni: with k features tested, a p-value only counts as significant below alpha / k,
    # so one feature out of fifty crossing 0.05 by chance is not reported as drift.
    alpha = settings.analysis_ks_pvalue_reuse_threshold / max(len(features), 1)
    significant = [f.feature for f in features if f.ks_pvalue < alpha]

    drifted = merged.drifted_count
    minimum = settings.decision_min_drifted_rows
    if not features:
        sufficient, note = False, "no feature could be compared between the two segments"
    elif drifted < minimum:
        sufficient = False
        note = f"only {drifted} drifted rows; at least {minimum} are needed to act on the drift"
    else:
        sufficient, note = True, f"{drifted} drifted rows (minimum {minimum})"

    return DriftSummary(
        features_compared=len(features),
        affected_features=affected,
        n_affected=len(affected),
        affected_share=len(affected) / len(features) if features else 0.0,
        max_psi=comparison.max_psi,
        mean_psi=sum(f.psi for f in features) / len(features) if features else 0.0,
        min_ks_pvalue=comparison.min_ks_pvalue,
        bonferroni_alpha=alpha,
        significant_features=significant,
        historical_rows=merged.historical_count,
        drifted_rows=drifted,
        late_rows=merged.overlap_count,
        sample_sufficient=sufficient,
        sample_note=note,
        previous_version=previous_version,
        recent_performance=dict(recent_performance),
        capabilities=framework_capabilities(framework, settings),
    )
