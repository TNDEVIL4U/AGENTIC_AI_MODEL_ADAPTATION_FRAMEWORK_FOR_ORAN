"""Member 1 - reuse: decide whether the current model can be kept as-is.

A caller-reported DriftEvent is a hypothesis, not a verdict: this module corroborates it (or
doesn't) against the real comparison statistics before deciding to hand the job on to Member 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from oran_adapt.analysis.comparison import ComparisonResult, FeatureComparison
from oran_adapt.analysis.schemas import FeatureShift
from oran_adapt.core.schemas import DriftEvent


@dataclass
class ReuseAssessment:
    reuse: bool
    reason: str


def is_shifted(
    feature: FeatureComparison | FeatureShift,
    *,
    psi_threshold: float,
    ks_pvalue_threshold: float,
    min_psi_rows: int,
) -> bool:
    """A feature has shifted when its KS test is significant, or when its PSI crosses the
    threshold on at least ``min_psi_rows`` rows in both segments. PSI bins a handful of rows
    into mostly-empty buckets and swings wildly, so on its own, on a small sample, it is noise."""
    if feature.ks_pvalue < ks_pvalue_threshold:
        return True
    enough = min(feature.n_historical, feature.n_drifted) >= min_psi_rows
    return enough and feature.psi >= psi_threshold


def assess_reuse(
    event: DriftEvent,
    comparison: ComparisonResult,
    *,
    psi_threshold: float,
    ks_pvalue_threshold: float,
    drift_score_threshold: float,
    min_psi_rows: int = 30,
) -> ReuseAssessment:
    if not event.drift_detected:
        return ReuseAssessment(reuse=True, reason="caller reported no drift")

    if event.drift_score is not None and event.drift_score >= drift_score_threshold:
        return ReuseAssessment(
            reuse=False,
            reason=(
                f"reported drift_score {event.drift_score:.3f} >= threshold {drift_score_threshold}"
            ),
        )

    if not comparison.features:
        # Drift was reported but there is no comparable data to corroborate or refute it with;
        # stay conservative and do not reuse.
        return ReuseAssessment(
            reuse=False, reason="drift reported and no comparable feature statistics available"
        )

    shifted = [
        f
        for f in comparison.features
        if is_shifted(
            f,
            psi_threshold=psi_threshold,
            ks_pvalue_threshold=ks_pvalue_threshold,
            min_psi_rows=min_psi_rows,
        )
    ]
    if not shifted:
        return ReuseAssessment(
            reuse=True,
            reason="no feature crossed the PSI/KS reuse thresholds despite reported drift",
        )

    names = ", ".join(f.feature for f in shifted)
    return ReuseAssessment(reuse=False, reason=f"significant statistical shift in: {names}")
