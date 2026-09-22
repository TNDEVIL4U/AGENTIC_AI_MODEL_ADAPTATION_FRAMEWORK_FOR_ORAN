"""Phase 2: Member 1 (analysis engine) - retrieval, timestamp merge, comparison, reuse, package."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from oran_adapt.analysis.comparison import ComparisonResult, FeatureComparison, compare_segments
from oran_adapt.analysis.engine import analyze
from oran_adapt.analysis.merge import timestamp_merge
from oran_adapt.analysis.retrieval import DataSlice, retrieve_context
from oran_adapt.analysis.reuse import assess_reuse
from oran_adapt.core.enums import AssociationRole, DataKind
from oran_adapt.core.errors import ModelNotFoundError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
    PerformanceRecord,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ---- seed helpers ----------------------------------------------------------------------------
def _seed(
    session_factory,
    *,
    model_id: str = "ran-kpi-v1",
    historical_values: list[float] | None = None,
    drifted_values: list[float] | None = None,
    include_drifted: bool = True,
    include_historical: bool = True,
) -> None:
    with session_scope(session_factory) as session:
        session.add(
            ModelMetadata(
                model_id=model_id,
                mlflow_model_name="ran_kpi_model",
                model_type="regression",
                framework="sklearn",
            )
        )
        dataset = DatasetMetadata(dataset_id="kpi-ds", name="RAN KPIs", schema={})
        session.add(dataset)
        session.flush()

        if include_historical:
            values = historical_values if historical_values is not None else list(range(30))
            hist = DataVersion(
                dataset_id=dataset.id,
                version="hist-1",
                kind=DataKind.HISTORICAL,
                data_start=T0,
                data_end=T0 + timedelta(days=len(values)),
                row_count=len(values),
            )
            session.add(hist)
            session.flush()
            for i, v in enumerate(values):
                session.add(
                    DataRecord(
                        data_version_id=hist.id,
                        observed_at=T0 + timedelta(days=i),
                        payload={"prb_util": v},
                    )
                )
            session.add(
                ModelDataAssociation(
                    model_id=model_id,
                    model_version="1",
                    data_version_id=hist.id,
                    role=AssociationRole.TRAINING,
                )
            )

        if include_drifted:
            values = drifted_values if drifted_values is not None else list(range(30))
            base = T0 + timedelta(days=100)
            drift = DataVersion(
                dataset_id=dataset.id,
                version="drift-1",
                kind=DataKind.DRIFTED,
                data_start=base,
                data_end=base + timedelta(days=len(values)),
                row_count=len(values),
            )
            session.add(drift)
            session.flush()
            for i, v in enumerate(values):
                session.add(
                    DataRecord(
                        data_version_id=drift.id,
                        observed_at=base + timedelta(days=i),
                        payload={"prb_util": v},
                    )
                )
            session.add(
                ModelDataAssociation(
                    model_id=model_id,
                    model_version="1",
                    data_version_id=drift.id,
                    role=AssociationRole.DRIFT_OBSERVED,
                )
            )

        session.add(
            PerformanceRecord(
                model_id=model_id, model_version="1", metric_name="rmse", value=1.23
            )
        )


@pytest.fixture
def session_factory(migrated_settings):
    engine = create_db_engine(migrated_settings.database_url)
    return make_session_factory(engine)


# ---- retrieval --------------------------------------------------------------------------------
def test_retrieval_finds_model_and_both_slices(session_factory) -> None:
    _seed(session_factory)
    with session_scope(session_factory) as session:
        ctx = retrieve_context(session, DriftEvent(model_id="ran-kpi-v1", drift_detected=True))
    assert ctx.model.model_id == "ran-kpi-v1"
    assert ctx.historical is not None and ctx.historical.row_count == 30
    assert ctx.drifted is not None and ctx.drifted.row_count == 30
    assert ctx.historical.kind == DataKind.HISTORICAL
    assert ctx.drifted.kind == DataKind.DRIFTED
    assert len(ctx.recent_performance) == 1


def test_retrieval_raises_when_model_unknown(session_factory) -> None:
    with session_scope(session_factory) as session, pytest.raises(ModelNotFoundError) as ei:
        retrieve_context(session, DriftEvent(model_id="does-not-exist", drift_detected=True))
    assert ei.value.code == "MODEL_NOT_FOUND"


def test_retrieval_returns_none_slices_when_no_data(session_factory) -> None:
    with session_scope(session_factory) as session:
        session.add(ModelMetadata(model_id="bare-model", mlflow_model_name="bare"))
    with session_scope(session_factory) as session:
        ctx = retrieve_context(session, DriftEvent(model_id="bare-model", drift_detected=True))
    assert ctx.historical is None
    assert ctx.drifted is None


def test_retrieval_resolves_drifted_version_by_explicit_reference(session_factory) -> None:
    _seed(session_factory, historical_values=list(range(10)), drifted_values=list(range(10)))
    with session_scope(session_factory) as session:
        event = DriftEvent(
            model_id="ran-kpi-v1",
            drift_detected=True,
            dataset_id="kpi-ds",
            drifted_data_version="drift-1",
        )
        ctx = retrieve_context(session, event)
    assert ctx.drifted is not None
    assert ctx.drifted.version == "drift-1"


# ---- timestamp merge ---------------------------------------------------------------------------
def _slice(records: list[dict]) -> DataSlice:
    return DataSlice(
        data_version_id=1,
        version="v1",
        kind="HISTORICAL",
        row_count=len(records),
        data_start=records[0]["observed_at"] if records else None,
        data_end=records[-1]["observed_at"] if records else None,
        records=records,
    )


def test_timestamp_merge_orders_rows_and_sets_boundary() -> None:
    hist = _slice(
        [
            {"observed_at": T0 + timedelta(days=1), "x": 1},
            {"observed_at": T0, "x": 0},
        ]
    )
    drift = _slice([{"observed_at": T0 + timedelta(days=5), "x": 9}])
    merged = timestamp_merge(hist, drift)
    assert [r["x"] for r in merged.rows] == [0, 1, 9]
    assert merged.boundary_at == T0 + timedelta(days=1)
    assert merged.historical_count == 2
    assert merged.drifted_count == 1
    assert merged.overlap_count == 0


def test_timestamp_merge_flags_drifted_rows_at_or_before_boundary() -> None:
    hist = _slice([{"observed_at": T0 + timedelta(days=1), "x": 1}])
    drift = _slice(
        [
            {"observed_at": T0, "x": -1},  # before boundary -> overlap
            {"observed_at": T0 + timedelta(days=1), "x": 0},  # equal to boundary -> overlap
            {"observed_at": T0 + timedelta(days=2), "x": 2},  # after boundary -> fine
        ]
    )
    merged = timestamp_merge(hist, drift)
    assert merged.overlap_count == 2


def test_timestamp_merge_handles_missing_historical() -> None:
    drift = _slice([{"observed_at": T0, "x": 1}])
    merged = timestamp_merge(None, drift)
    assert merged.boundary_at is None
    assert merged.historical_count == 0
    assert merged.overlap_count == 0


# ---- comparison ---------------------------------------------------------------------------------
def test_comparison_detects_significant_feature_shift() -> None:
    hist = _slice(
        [{"observed_at": T0 + timedelta(days=i), "prb_util": float(i)} for i in range(30)]
    )
    drift = _slice(
        [
            {"observed_at": T0 + timedelta(days=100 + i), "prb_util": float(i) + 100.0}
            for i in range(30)
        ]
    )
    merged = timestamp_merge(hist, drift)
    result = compare_segments(merged)
    assert len(result.features) == 1
    fc = result.features[0]
    assert fc.feature == "prb_util"
    assert fc.ks_pvalue < 0.05
    assert fc.psi > 0.1
    assert result.max_psi == fc.psi
    assert result.min_ks_pvalue == fc.ks_pvalue


def test_comparison_reports_no_shift_for_identical_distributions() -> None:
    values = [float(i) for i in range(30)]
    hist = _slice([{"observed_at": T0 + timedelta(days=i), "prb_util": v} for i, v in enumerate(values)])
    drift = _slice(
        [{"observed_at": T0 + timedelta(days=100 + i), "prb_util": v} for i, v in enumerate(values)]
    )
    merged = timestamp_merge(hist, drift)
    result = compare_segments(merged)
    fc = result.features[0]
    assert fc.psi == pytest.approx(0.0, abs=1e-9)
    assert fc.ks_pvalue == pytest.approx(1.0)


def test_comparison_empty_when_one_segment_missing() -> None:
    hist = _slice([{"observed_at": T0, "prb_util": 1.0}])
    merged = timestamp_merge(hist, None)
    result = compare_segments(merged)
    assert result.features == []


def test_comparison_ignores_non_numeric_features() -> None:
    hist = _slice(
        [{"observed_at": T0 + timedelta(days=i), "prb_util": float(i), "cell": "A"} for i in range(10)]
    )
    drift = _slice(
        [
            {"observed_at": T0 + timedelta(days=100 + i), "prb_util": float(i), "cell": "B"}
            for i in range(10)
        ]
    )
    merged = timestamp_merge(hist, drift)
    result = compare_segments(merged)
    assert {f.feature for f in result.features} == {"prb_util"}


# ---- reuse ----------------------------------------------------------------------------------
_THRESHOLDS = {"psi_threshold": 0.1, "ks_pvalue_threshold": 0.05, "drift_score_threshold": 0.3}


def test_reuse_true_when_no_drift_detected() -> None:
    event = DriftEvent(model_id="m", drift_detected=False)
    assessment = assess_reuse(event, ComparisonResult(), **_THRESHOLDS)
    assert assessment.reuse is True


def test_reuse_false_when_reported_drift_score_high() -> None:
    event = DriftEvent(model_id="m", drift_detected=True, drift_score=0.9)
    assessment = assess_reuse(event, ComparisonResult(), **_THRESHOLDS)
    assert assessment.reuse is False
    assert "drift_score" in assessment.reason


def test_reuse_false_when_drift_reported_with_no_comparable_features() -> None:
    event = DriftEvent(model_id="m", drift_detected=True)
    assessment = assess_reuse(event, ComparisonResult(), **_THRESHOLDS)
    assert assessment.reuse is False
    assert "no comparable" in assessment.reason


def test_reuse_true_when_stats_dont_corroborate_reported_drift() -> None:
    event = DriftEvent(model_id="m", drift_detected=True)
    comparison = ComparisonResult(
        features=[
            FeatureComparison(
                feature="prb_util",
                historical_mean=1.0,
                drifted_mean=1.01,
                historical_std=0.1,
                drifted_std=0.1,
                ks_statistic=0.05,
                ks_pvalue=0.9,
                psi=0.01,
            )
        ],
        max_psi=0.01,
        min_ks_pvalue=0.9,
    )
    assessment = assess_reuse(event, comparison, **_THRESHOLDS)
    assert assessment.reuse is True
    assert "did not" not in assessment.reason  # sanity: real reason string, not a stub
    assert "PSI/KS" in assessment.reason


def test_reuse_false_when_stats_confirm_shift() -> None:
    event = DriftEvent(model_id="m", drift_detected=True)
    comparison = ComparisonResult(
        features=[
            FeatureComparison(
                feature="prb_util",
                historical_mean=1.0,
                drifted_mean=50.0,
                historical_std=0.1,
                drifted_std=0.1,
                ks_statistic=1.0,
                ks_pvalue=0.0001,
                psi=2.5,
            )
        ],
        max_psi=2.5,
        min_ks_pvalue=0.0001,
    )
    assessment = assess_reuse(event, comparison, **_THRESHOLDS)
    assert assessment.reuse is False
    assert "prb_util" in assessment.reason


# ---- end-to-end analyze() ---------------------------------------------------------------------
def test_analyze_raises_model_not_found(session_factory, migrated_settings) -> None:
    with session_scope(session_factory) as session, pytest.raises(ModelNotFoundError):
        analyze(session, DriftEvent(model_id="nope", drift_detected=True), migrated_settings)


def test_analyze_returns_insufficient_data_without_drifted_data(
    session_factory, migrated_settings
) -> None:
    _seed(session_factory, include_drifted=False)
    with session_scope(session_factory) as session:
        result = analyze(
            session, DriftEvent(model_id="ran-kpi-v1", drift_detected=True), migrated_settings
        )
    assert result.status == "INSUFFICIENT_DATA"
    assert result.reuse is False
    assert result.decision_package is None


def test_analyze_reuses_when_no_drift_detected(session_factory, migrated_settings) -> None:
    _seed(session_factory)
    with session_scope(session_factory) as session:
        result = analyze(
            session, DriftEvent(model_id="ran-kpi-v1", drift_detected=False), migrated_settings
        )
    assert result.status == "REUSE"
    assert result.reuse is True
    assert result.decision_package is None


def test_analyze_packages_decision_when_drift_confirmed(session_factory, migrated_settings) -> None:
    _seed(
        session_factory,
        historical_values=[float(i) for i in range(30)],
        drifted_values=[float(i) + 200.0 for i in range(30)],
    )
    with session_scope(session_factory) as session:
        event = DriftEvent(model_id="ran-kpi-v1", drift_detected=True, drift_score=0.95)
        result = analyze(session, event, migrated_settings)

    assert result.status == "PACKAGED"
    assert result.reuse is False
    pkg = result.decision_package
    assert pkg is not None
    assert pkg.model_id == "ran-kpi-v1"
    assert pkg.model_type == "regression"
    assert pkg.historical_data.row_count == 30
    assert pkg.drifted_data.row_count == 30
    assert pkg.feature_shifts and pkg.feature_shifts[0].feature == "prb_util"
    assert pkg.max_psi > 0
    assert pkg.recent_performance == {"rmse": 1.23}
    # Must be JSON-serializable end to end: this is what gets stored on AdaptationJob.result.
    json.dumps(pkg.model_dump(mode="json"))
