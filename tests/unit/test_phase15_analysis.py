"""Phase D (Rule 5): Member 1 hands the decision engine everything it needs to judge the drift
- magnitude, affected features, sample sizes, significance, recent performance, the previous
version and the available capabilities - and PSI alone, on a sample too small for it to mean
anything, never marks a feature as shifted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oran_adapt.analysis.comparison import ComparisonResult, FeatureComparison, compare_segments
from oran_adapt.analysis.merge import MergedSeries, timestamp_merge
from oran_adapt.analysis.retrieval import DataSlice
from oran_adapt.analysis.reuse import assess_reuse, is_shifted
from oran_adapt.analysis.schemas import DataVersionRef, DecisionPackage, FeatureShift
from oran_adapt.analysis.summary import framework_capabilities, summarize_drift
from oran_adapt.core.config import Settings
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.decision.engine import decide

T0 = datetime(2026, 1, 1, tzinfo=UTC)
THRESHOLDS = {"psi_threshold": 0.1, "ks_pvalue_threshold": 0.05, "min_psi_rows": 30}


def _feature(name="prb_util", *, psi=0.0, ks_pvalue=1.0, n=40) -> FeatureComparison:
    return FeatureComparison(
        feature=name,
        historical_mean=1.0,
        drifted_mean=2.0,
        historical_std=0.1,
        drifted_std=0.1,
        ks_statistic=0.5,
        ks_pvalue=ks_pvalue,
        psi=psi,
        n_historical=n,
        n_drifted=n,
    )


def _comparison(*features: FeatureComparison) -> ComparisonResult:
    return ComparisonResult(
        features=list(features),
        historical_count=max((f.n_historical for f in features), default=0),
        drifted_count=max((f.n_drifted for f in features), default=0),
        max_psi=max((f.psi for f in features), default=0.0),
        min_ks_pvalue=min((f.ks_pvalue for f in features), default=1.0),
    )


def _merged(historical: int, drifted: int, late: int = 0) -> MergedSeries:
    return MergedSeries(
        rows=[],
        historical_count=historical,
        drifted_count=drifted,
        boundary_at=T0,
        overlap_count=late,
    )


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---- PSI is never used alone ------------------------------------------------------------------
def test_high_psi_on_a_small_sample_without_significance_is_not_a_shift():
    small = _feature(psi=0.9, ks_pvalue=0.3, n=10)
    assert not is_shifted(small, **THRESHOLDS)

    assessment = assess_reuse(
        DriftEvent(model_id="m", drift_detected=True),
        _comparison(small),
        drift_score_threshold=0.3,
        **THRESHOLDS,
    )
    assert assessment.reuse is True


def test_high_psi_on_a_sample_large_enough_is_a_shift():
    assert is_shifted(_feature(psi=0.9, ks_pvalue=0.3, n=30), **THRESHOLDS)


def test_a_significant_ks_test_is_a_shift_whatever_the_sample_size():
    assert is_shifted(_feature(psi=0.01, ks_pvalue=0.001, n=8), **THRESHOLDS)


def test_comparison_records_the_sample_size_behind_each_feature():
    hist = [{"observed_at": T0 + timedelta(days=i), "prb_util": float(i)} for i in range(12)]
    drift = [
        {
            "observed_at": T0 + timedelta(days=50 + i),
            "prb_util": float(i) + 40.0,
            **({"cqi": float(i)} if i < 5 else {}),
        }
        for i in range(20)
    ]
    hist = [{**r, "cqi": float(i)} for i, r in enumerate(hist)]

    def _slice(records, kind):
        return DataSlice(
            data_version_id=1,
            version=kind,
            kind=kind,
            row_count=len(records),
            data_start=None,
            data_end=None,
            records=records,
        )

    result = compare_segments(timestamp_merge(_slice(hist, "H"), _slice(drift, "D")))
    sizes = {f.feature: (f.n_historical, f.n_drifted) for f in result.features}
    assert sizes == {"cqi": (12, 5), "prb_util": (12, 20)}


# ---- the drift summary ------------------------------------------------------------------------
def test_summary_carries_magnitude_affected_features_sizes_and_significance():
    comparison = _comparison(
        _feature("prb_util", psi=0.8, ks_pvalue=0.0001),
        _feature("cqi", psi=0.15, ks_pvalue=0.03),
        _feature("rsrp", psi=0.02, ks_pvalue=0.7),
    )
    summary = summarize_drift(
        comparison,
        _merged(200, 40, late=3),
        settings=_settings(),
        framework="sklearn",
        recent_performance={"rmse": 4.2},
        previous_version="3",
    )

    assert summary.features_compared == 3
    assert summary.affected_features == ["prb_util", "cqi"]
    assert summary.n_affected == 2
    assert summary.affected_share == 2 / 3
    assert summary.max_psi == 0.8
    assert abs(summary.mean_psi - (0.8 + 0.15 + 0.02) / 3) < 1e-12
    assert summary.min_ks_pvalue == 0.0001
    # Three KS tests at 0.05: only p < 0.05 / 3 survives the multiple-testing correction.
    assert abs(summary.bonferroni_alpha - 0.05 / 3) < 1e-12
    assert summary.significant_features == ["prb_util"]
    assert (summary.historical_rows, summary.drifted_rows, summary.late_rows) == (200, 40, 3)
    assert summary.sample_sufficient is True
    assert summary.previous_version == "3"
    assert summary.recent_performance == {"rmse": 4.2}
    assert summary.capabilities["full_retraining"] is True


def test_summary_flags_a_drifted_sample_too_small_to_act_on():
    summary = summarize_drift(
        _comparison(_feature(psi=0.9, ks_pvalue=0.001, n=5)),
        _merged(100, 5),
        settings=_settings(),
        framework="sklearn",
        recent_performance={},
        previous_version=None,
    )
    assert summary.sample_sufficient is False
    assert "5 drifted rows" in summary.sample_note


def test_capabilities_follow_the_framework_and_llm_configuration():
    s = _settings()
    assert framework_capabilities("sklearn", s) == {
        "fine_tuning": True,
        "full_retraining": True,
        "llm_adapter": False,
    }
    assert framework_capabilities("PyTorch", s)["fine_tuning"] is True
    assert framework_capabilities("xgboost", s) == {
        "fine_tuning": False,
        "full_retraining": True,
        "llm_adapter": False,
    }
    assert framework_capabilities("onnx", s) == {
        "fine_tuning": False,
        "full_retraining": False,
        "llm_adapter": False,
    }
    assert framework_capabilities(None, s)["full_retraining"] is False


# ---- the decision engine reads the summary ----------------------------------------------------
def test_decision_evidence_lists_the_statistically_affected_features_not_psi_alone():
    comparison = _comparison(
        _feature("prb_util", psi=0.8, ks_pvalue=0.0001),
        _feature("cqi", psi=0.5, ks_pvalue=0.4, n=10),
    )
    settings = _settings()
    summary = summarize_drift(
        comparison,
        _merged(100, 40),
        settings=settings,
        framework="sklearn",
        recent_performance={"rmse": 1.0},
        previous_version="1",
    )
    package = DecisionPackage(
        model_id="m",
        framework="sklearn",
        drift_event=DriftEvent(model_id="m", drift_detected=True),
        reuse_reason="shift",
        feature_shifts=[FeatureShift(**vars(f)) for f in comparison.features],
        drifted_data=DataVersionRef(data_version_id=1, version="d", kind="DRIFTED", row_count=40),
        max_psi=0.8,
        min_ks_pvalue=0.0001,
        recent_performance={"rmse": 1.0},
        drift_summary=summary,
    )

    decision = decide(package, settings, None)

    assert decision.evidence["drifted_features"] == ["prb_util"]
    assert decision.evidence["drift_summary"]["n_affected"] == 1
    assert decision.evidence["drift_summary"]["previous_version"] == "1"
