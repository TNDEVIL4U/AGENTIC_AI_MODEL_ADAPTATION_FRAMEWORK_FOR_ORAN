"""Member 1 - reuse: decide whether the current model can be kept as-is.

A caller-reported DriftEvent is a hypothesis, not a verdict: this module corroborates it (or
doesn't) against the real comparison statistics before deciding to hand the job on to Member 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from oran_adapt.analysis.comparison import ComparisonResult
from oran_adapt.core.schemas import DriftEvent


@dataclass
class ReuseAssessment:
    reuse: bool
    reason: str


def assess_reuse(
    event: DriftEvent,
    comparison: ComparisonResult,
    *,
    psi_threshold: float,
    ks_pvalue_threshold: float,
    drift_score_threshold: float,
) -> ReuseAssessment:
    if not event.drift_detected:
        return ReuseAssessment(reuse=True, reason="caller reported no drift")

    if event.drift_score is not None and event.drift_score >= drift_score_threshold:
        return ReuseAssessment(
            reuse=False,
            reason=(
                f"reported drift_score {event.drift_score:.3f} "
                f">= threshold {drift_score_threshold}"
            ),
        )

    if not comparison.features:
        # Drift was reported but there is no comparable data to corroborate or refute it with;
        # stay conservative and do not reuse.
        return ReuseAssessment(
            reuse=False, reason="drift reported and no comparable feature statistics available"
        )

    shifted = [
        f for f in comparison.features if f.psi >= psi_threshold or f.ks_pvalue < ks_pvalue_threshold
    ]
    if not shifted:
        return ReuseAssessment(
            reuse=True,
            reason="no feature crossed the PSI/KS reuse thresholds despite reported drift",
        )

    names = ", ".join(f.feature for f in shifted)
    return ReuseAssessment(reuse=False, reason=f"significant statistical shift in: {names}")
