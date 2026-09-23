"""Member 1 - analysis engine entry point: retrieval -> timestamp merge -> comparison -> reuse
-> package.

This is the single call the orchestrator (Phase 9) makes into Member 1. It either concludes the
job needs no action (REUSE), hands a fully-formed DecisionPackage to Member 2 (PACKAGED), or
reports that there isn't enough data to say anything yet (INSUFFICIENT_DATA). It never decides a
strategy itself - that is Member 2's job.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from oran_adapt.analysis.comparison import compare_segments
from oran_adapt.analysis.merge import timestamp_merge
from oran_adapt.analysis.retrieval import DataSlice, retrieve_context
from oran_adapt.analysis.reuse import assess_reuse
from oran_adapt.analysis.schemas import (
    AnalysisResult,
    DataVersionRef,
    DecisionPackage,
    FeatureShift,
)
from oran_adapt.core.config import Settings
from oran_adapt.core.schemas import DriftEvent


def _ref(data_slice: DataSlice | None) -> DataVersionRef | None:
    if data_slice is None:
        return None
    return DataVersionRef(
        data_version_id=data_slice.data_version_id,
        version=data_slice.version,
        kind=data_slice.kind,
        row_count=data_slice.row_count,
        data_start=data_slice.data_start,
        data_end=data_slice.data_end,
    )


def analyze(
    session: Session, event: DriftEvent, settings: Settings, *, live_version: str | None = None
) -> AnalysisResult:
    """Run the full Member 1 pipeline for a single drift event. Raises ModelNotFoundError if
    ``event.model_id`` is not registered. ``live_version`` picks the baseline: the data that
    version was trained on."""
    context = retrieve_context(session, event, live_version=live_version)

    if context.historical is None or context.drifted is None:
        missing = "historical" if context.historical is None else "drifted"
        return AnalysisResult(
            status="INSUFFICIENT_DATA",
            model_id=event.model_id,
            reuse=False,
            reason=f"no {missing} data available for comparison",
        )

    merged = timestamp_merge(context.historical, context.drifted)
    comparison = compare_segments(merged)
    assessment = assess_reuse(
        event,
        comparison,
        psi_threshold=settings.analysis_psi_reuse_threshold,
        ks_pvalue_threshold=settings.analysis_ks_pvalue_reuse_threshold,
        drift_score_threshold=settings.analysis_drift_score_reuse_threshold,
    )

    if assessment.reuse:
        return AnalysisResult(
            status="REUSE",
            model_id=event.model_id,
            reuse=True,
            reason=assessment.reason,
        )

    package = DecisionPackage(
        model_id=event.model_id,
        model_type=context.model.model_type,
        framework=context.model.framework,
        drift_event=event,
        reuse_reason=assessment.reason,
        historical_data=_ref(context.historical),
        drifted_data=_ref(context.drifted),
        feature_shifts=[FeatureShift(**vars(f)) for f in comparison.features],
        max_psi=comparison.max_psi,
        min_ks_pvalue=comparison.min_ks_pvalue,
        merge_overlap_count=merged.overlap_count,
        recent_performance={p.metric_name: p.value for p in context.recent_performance},
    )
    return AnalysisResult(
        status="PACKAGED",
        model_id=event.model_id,
        reuse=False,
        reason=assessment.reason,
        decision_package=package,
    )
