"""Member 1 - analysis engine entry point: retrieval -> timestamp merge -> comparison ->
package.

This is the single call the orchestrator (Phase 9) makes into Member 1. A drift event that the
caller reports is acted on: the comparison statistics are packaged as severity evidence for the
Similar Model Exchange and the Decision Engine (PACKAGED). The job needs no action only when the
caller itself reports no drift (REUSE), and stops when there is not enough data to compare
(INSUFFICIENT_DATA). It never decides a strategy itself - that is Member 2's job.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from oran_adapt.analysis.comparison import compare_segments
from oran_adapt.analysis.merge import timestamp_merge
from oran_adapt.analysis.retrieval import DataSlice, retrieve_context
from oran_adapt.analysis.schemas import (
    AnalysisResult,
    DataVersionRef,
    DecisionPackage,
    FeatureShift,
)
from oran_adapt.analysis.summary import summarize_drift
from oran_adapt.core.config import Settings
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.datastore.access import DataAccess


def _ref(data_slice: DataSlice | None) -> DataVersionRef | None:
    if data_slice is None:
        return None
    return DataVersionRef(
        data_version_id=data_slice.data_version_id,
        version=data_slice.version,
        kind=data_slice.kind,
        row_count=data_slice.total_rows
        if data_slice.total_rows is not None
        else data_slice.row_count,
        data_start=data_slice.data_start,
        data_end=data_slice.data_end,
    )


def analyze(
    session: Session,
    event: DriftEvent,
    settings: Settings,
    *,
    live_version: str | None = None,
    access: DataAccess | None = None,
) -> AnalysisResult:
    """Run the full Member 1 pipeline for a single drift event. Raises ModelNotFoundError if
    ``event.model_id`` is not registered. ``live_version`` picks the baseline: the data that
    version was trained on."""
    context = retrieve_context(
        session,
        event,
        performance_limit=settings.analysis_performance_history_limit,
        live_version=live_version,
        access=access or DataAccess.from_settings(settings),
    )

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

    if not event.drift_detected:
        return AnalysisResult(
            status="REUSE",
            model_id=event.model_id,
            reuse=True,
            reason="caller reported no drift",
        )
    reason = "drift reported by the caller; statistics passed on as severity evidence"

    recent_performance = {p.metric_name: p.value for p in context.recent_performance}
    package = DecisionPackage(
        model_id=event.model_id,
        model_type=context.model.model_type,
        framework=context.model.framework,
        task_type=context.model.task_type,
        drift_event=event,
        reuse_reason=reason,
        historical_data=_ref(context.historical),
        drifted_data=_ref(context.drifted),
        feature_shifts=[FeatureShift(**vars(f)) for f in comparison.features],
        max_psi=comparison.max_psi,
        min_ks_pvalue=comparison.min_ks_pvalue,
        merge_overlap_count=merged.overlap_count,
        recent_performance=recent_performance,
        drift_summary=summarize_drift(
            comparison,
            merged,
            settings=settings,
            framework=context.model.framework,
            recent_performance=recent_performance,
            previous_version=live_version,
        ),
    )
    return AnalysisResult(
        status="PACKAGED",
        model_id=event.model_id,
        reuse=False,
        reason=reason,
        decision_package=package,
    )
