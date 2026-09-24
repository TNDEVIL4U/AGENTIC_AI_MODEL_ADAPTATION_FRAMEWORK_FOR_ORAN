"""Phase 14 Stage D: task-agnostic metrics, the full Decision Engine output, and leakage checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression, LogisticRegression
from test_phase14_member1 import (  # shared seeding helpers (same test directory)
    FEATURES,
    TARGET,
    _ev,
    _rsrp_regime,
    _seed,
    _seed_three_similar,
    _submit,
)

from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.leakage import check_leakage
from oran_adapt.analysis.reuse_decision import decide_reuse
from oran_adapt.analysis.schemas import (
    DataVersionRef,
    DecisionPackage,
    FeatureShift,
    VersionEvaluation,
)
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import JobStatus, ReuseVerdict, Strategy, TaskType
from oran_adapt.core.errors import DataLeakageError, ValidationFailedError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import AuditLog, DataRecord
from oran_adapt.decision.engine import decide
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.validation.evaluate import evaluate_model
from oran_adapt.validation.metrics import (
    compute_metrics,
    higher_is_better,
    prediction_shift,
    resolve_task,
)

T0 = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def plain_settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


def _frame(n: int = 80, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(size=(n, 3)), columns=["a", "b", "c"])


# ---- metric system ---------------------------------------------------------------------------
def test_binary_classifier_gets_prf_and_ranking_metrics_with_accuracy_first() -> None:
    X = _frame()
    y = (X.a > 0).astype(int)
    scores = evaluate_model(
        LogisticRegression().fit(X, y), X, y, framework="sklearn", estimator_type="classifier"
    )
    assert next(iter(scores)) == "accuracy"
    assert {"precision", "recall", "f1", "roc_auc", "pr_auc"} <= set(scores)
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_multiclass_has_weighted_prf_and_no_invented_auc() -> None:
    y = np.array([0, 1, 2, 0, 1, 2, 0, 1])
    pred = np.array([0, 1, 1, 0, 1, 2, 2, 1])
    scores = compute_metrics(TaskType.CLASSIFICATION, y, pred, y_score=np.linspace(0, 1, 8))
    assert scores["accuracy"] == pytest.approx(6 / 8)
    assert {"precision", "recall", "f1"} <= set(scores)
    assert "roc_auc" not in scores and "pr_auc" not in scores


def test_regression_and_forecasting_metrics_only_where_defined() -> None:
    y = np.array([1.0, 2.0, 3.0, 4.0])
    pred = np.array([1.5, 2.0, 2.5, 4.0])
    reg = compute_metrics(TaskType.REGRESSION, y, pred)
    assert next(iter(reg)) == "rmse"
    assert {"mae", "mse", "rmse", "r2", "mape"} <= set(reg)
    assert reg["mae"] == pytest.approx(0.25)

    with_zero = compute_metrics(TaskType.REGRESSION, np.array([0.0, 1.0]), np.array([0.1, 1.0]))
    assert "mape" not in with_zero  # undefined with a zero target, so left out

    fc = compute_metrics(TaskType.FORECASTING, y, pred, y_train=[1.0, 2.0, 4.0, 3.0])
    assert {"mae", "rmse", "mape", "smape", "mase"} <= set(fc)
    assert "mase" not in compute_metrics(TaskType.FORECASTING, y, pred)


def test_forecaster_is_scored_with_its_task_metrics() -> None:
    X = _frame()
    y = X.a * 3 + 10
    scores = evaluate_model(
        LinearRegression().fit(X, y), X, y, framework="sklearn", estimator_type="regressor",
        task_type="forecasting", y_train=y,
    )
    assert next(iter(scores)) == "rmse" and "smape" in scores and "mase" in scores


def test_clustering_and_anomaly_models_are_scorable() -> None:
    X = _frame(120)
    kmeans = KMeans(3, n_init=2, random_state=0).fit(X)
    assert inspect_model(kmeans, "sklearn").estimator_type == "clusterer"
    scores = evaluate_model(kmeans, X, None, framework="sklearn", estimator_type="clusterer")
    assert next(iter(scores)) == "silhouette" and "davies_bouldin" in scores

    # One cluster: silhouette is undefined, so the model cannot be compared - refused, not faked.
    one = KMeans(1, n_init=1, random_state=0).fit(X)
    with pytest.raises(ValidationFailedError):
        evaluate_model(one, X, None, framework="sklearn", estimator_type="clusterer")

    forest = IsolationForest(random_state=0).fit(X)
    assert inspect_model(forest, "sklearn").estimator_type == "outlier_detector"
    labels = pd.Series((X.a.abs() > 1.8).astype(int))  # 1 = anomaly
    scores = evaluate_model(forest, X, labels, framework="sklearn", estimator_type="outlier_detector")
    assert next(iter(scores)) == "f1" and {"precision", "recall", "pr_auc"} <= set(scores)


def test_without_labels_there_is_no_performance_metric_only_prediction_shift() -> None:
    assert compute_metrics(TaskType.CLASSIFICATION, None, np.array([0, 1])) == {}
    rng = np.random.default_rng(1)
    shift = prediction_shift(rng.normal(size=200), rng.normal(1.0, 1.0, size=200))
    assert shift["prediction_psi"] > 0.1 and shift["prediction_mean_shift"] > 0.5
    assert prediction_shift(["a", "b"] * 5, ["a"] * 10) == {"prediction_tvd": 0.5}


def test_task_resolution_and_metric_direction() -> None:
    assert resolve_task("forecasting", "regressor") == TaskType.FORECASTING
    assert resolve_task("Anomaly-Detection", "outlier_detector") == TaskType.ANOMALY_DETECTION
    # A recorded task that contradicts the artifact loses to what the artifact is.
    assert resolve_task("classification", "regressor") == TaskType.REGRESSION
    assert resolve_task(None, "classifier") == TaskType.CLASSIFICATION
    with pytest.raises(ValueError):
        resolve_task(None, "unknown")
    assert higher_is_better("f1") and higher_is_better("silhouette")
    assert not higher_is_better("rmse") and not higher_is_better("davies_bouldin")
    with pytest.raises(ValueError):
        higher_is_better("made_up")


def test_reuse_rule_works_on_non_accuracy_metrics_and_explains_itself(settings) -> None:
    evals = [
        _ev("1", 0.60, metric="f1"),
        _ev("2", 0.70, live=True, metric="f1"),
        _ev("3", 0.80, metric="f1"),
    ]
    decision = decide_reuse(evals, live_version="2", max_psi=0.2, settings=settings)
    assert decision.verdict == ReuseVerdict.REUSE_EXISTING_VERSION
    assert decision.selected_version == "3"
    assert decision.thresholds["metric"] == "f1" and decision.thresholds["higher_is_better"]
    assert decision.thresholds["required_gain"] == settings.reuse_min_accuracy_gain
    assert decision.metrics == {"1": 0.60, "2": 0.70, "3": 0.80}
    assert decision.evidence["gains"]["3"] == pytest.approx(0.10)
    assert decision.evidence["max_psi"] == 0.2

    # Davies-Bouldin is an error-style metric: lower wins.
    evals = [_ev("1", 1.0, live=True, metric="davies_bouldin"), _ev("2", 0.5, metric="davies_bouldin")]
    decision = decide_reuse(evals, live_version="1", max_psi=0.2, settings=settings)
    assert decision.selected_version == "2"


# ---- decision engine output ------------------------------------------------------------------
def _package(**overrides) -> DecisionPackage:
    live = VersionEvaluation(
        version="3", is_live=True, compatible=True, metric_name="accuracy", metric_value=0.7,
        metrics={"accuracy": 0.7, "f1": 0.65}, baseline_metrics={"accuracy": 0.9},
        degradation=0.2, n_rows=40,
    )
    defaults = {
        "model_id": "cell-x",
        "model_type": "classification",
        "framework": "sklearn",
        "task_type": "classification",
        "drift_event": DriftEvent(model_id="cell-x", drift_score=0.4, severity="HIGH"),
        "reuse_reason": "shift in prb_util",
        "historical_data": DataVersionRef(
            data_version_id=1, version="hist-1", kind="HISTORICAL", row_count=200
        ),
        "drifted_data": DataVersionRef(
            data_version_id=2, version="drift-1", kind="DRIFTED", row_count=100
        ),
        "feature_shifts": [
            FeatureShift(
                feature="prb_util", historical_mean=0.5, drifted_mean=0.9, historical_std=0.1,
                drifted_std=0.1, ks_statistic=0.6, ks_pvalue=0.001, psi=0.3,
            )
        ],
        "max_psi": 0.3,
        "min_ks_pvalue": 0.001,
        "version_evaluations": [live],
    }
    defaults.update(overrides)
    return DecisionPackage(**defaults)


_OUTPUT_FIELDS = (
    "evidence", "thresholds", "expected_cost", "expected_improvement", "required_data",
    "resource_requirement",
)


def test_fallback_decision_carries_the_full_output(plain_settings) -> None:
    d = decide(_package(), plain_settings, None)
    assert (d.source, d.strategy) == ("FALLBACK", Strategy.FINE_TUNING)
    assert all(getattr(d, f) for f in _OUTPUT_FIELDS)
    assert d.rationale and 0.0 <= d.confidence <= 1.0
    assert d.fallback_strategy == Strategy.FULL_RETRAINING
    assert d.expected_cost["level"] == "LOW" and d.expected_cost["train_rows"] == 300
    assert d.expected_improvement["expected_gain"] == pytest.approx(0.2)
    assert d.expected_improvement["baseline_value"] == 0.9
    assert d.required_data["data_versions"] == ["hist-1", "drift-1"]
    assert d.resource_requirement["device"] == "cpu"
    assert d.resource_requirement["estimated_memory_mb"] > 0
    assert d.metrics == {"accuracy": 0.7, "f1": 0.65}
    assert d.evidence["severity"] == "HIGH" and d.evidence["drifted_features"] == ["prb_util"]
    assert d.thresholds["full_retrain_psi"] == plain_settings.decision_full_retrain_psi_threshold
    json.dumps(d.model_dump(mode="json"))  # serializable for the job result and audit trail


def test_hard_constraint_and_llm_decisions_are_explained_too(plain_settings) -> None:
    forced = decide(_package(framework="cobol"), plain_settings, None)
    assert forced.source == "HARD_CONSTRAINT"
    assert forced.fallback_strategy is None  # nothing was going to run
    assert forced.expected_cost["level"] == "NONE"
    assert forced.expected_improvement["expected_gain"] == 0.0

    class Llm:
        prompt = ""

        def complete(self, *, system: str, prompt: str) -> str:
            Llm.prompt = prompt
            return '{"strategy": "FULL_RETRAINING", "confidence": 0.8, "rationale": "big shift"}'

    d = decide(_package(), plain_settings, Llm())
    assert (d.source, d.strategy, d.confidence) == ("LLM", Strategy.FULL_RETRAINING, 0.8)
    assert d.expected_cost["level"] == "HIGH"
    # The LLM advises; the explanation still comes from the measured evidence.
    assert d.expected_improvement["expected_gain"] == pytest.approx(0.2)
    assert d.fallback_strategy in (Strategy.ROLLBACK, Strategy.NO_ACTION)
    sent = json.loads(Llm.prompt)
    assert sent["task_type"] == "classification"
    assert sent["version_evaluations"][0]["metrics"]["f1"] == 0.65
    assert sent["drifted_rows"] == 100


def test_unknown_degradation_gives_no_invented_improvement(plain_settings) -> None:
    d = decide(_package(version_evaluations=[]), plain_settings, None)
    assert d.expected_improvement["expected_gain"] is None
    assert "no measured basis" in d.expected_improvement["basis"]


# ---- leakage ---------------------------------------------------------------------------------
def _rec(rid: int, hours: int, **payload) -> DataRecord:
    return DataRecord(
        id=rid, data_version_id=1, observed_at=T0 + timedelta(hours=hours), payload=payload
    )


def test_leakage_drops_holdout_rows_copies_and_future_rows(plain_settings) -> None:
    holdout = [_rec(10, 10, x=1.0, y=0), _rec(11, 11, x=2.0, y=1)]
    train = [
        _rec(1, 1, x=5.0, y=0),
        _rec(2, 2, x=6.0, y=1),
        _rec(10, 10, x=1.0, y=0),  # the held-out row itself
        _rec(3, 3, x=2.0, y=1),  # a copy of a held-out row
        _rec(4, 12, x=7.0, y=0),  # observed after the hold-out starts
        _rec(5, 4, x=5.0, y=0),  # a duplicate inside the training rows (counted, kept)
    ]
    kept, report = check_leakage(
        train, holdout, target="y", feature_names=["x"], settings=plain_settings
    )
    assert [r.id for r in kept] == [1, 2, 5]
    assert (
        report.holdout_overlap_removed,
        report.holdout_duplicates_removed,
        report.future_rows_removed,
        report.train_duplicate_rows,
    ) == (1, 1, 1, 1)
    assert report.train_holdout_disjoint and report.split == "temporal"
    assert report.train_rows_in == 6 and report.train_rows_out == 3
    assert report.train_end < report.holdout_start

    allow = plain_settings.model_copy(update={"leakage_allow_future_rows": True})
    kept, report = check_leakage(train, holdout, target="y", feature_names=["x"], settings=allow)
    assert 4 in [r.id for r in kept] and report.future_rows_removed == 0
    assert report.split.startswith("unordered")

    off = plain_settings.model_copy(update={"leakage_checks_enabled": False})
    kept, report = check_leakage(train, holdout, target="y", feature_names=["x"], settings=off)
    assert len(kept) == 6 and report.checked is False


def test_target_leakage_fails_instead_of_training(plain_settings) -> None:
    train = [_rec(i, i, x=float(i), y=float(i), z=float(i % 3), w=2.0 * i + 1) for i in range(8)]
    holdout = [_rec(100, 50, x=1.5, y=0.0, z=0.0, w=0.0)]
    with pytest.raises(DataLeakageError, match="identical to the target"):
        check_leakage(train, holdout, target="y", feature_names=["x", "z"], settings=plain_settings)
    with pytest.raises(DataLeakageError, match="the target is a feature"):
        check_leakage(train, holdout, target="y", feature_names=["y", "z"], settings=plain_settings)
    # A perfectly correlated feature is only refused when the correlation limit is configured.
    kept, _ = check_leakage(
        train, holdout, target="y", feature_names=["w", "z"], settings=plain_settings
    )
    assert len(kept) == 8
    strict = plain_settings.model_copy(update={"leakage_target_correlation_max": 0.99})
    with pytest.raises(DataLeakageError, match="corr"):
        check_leakage(train, holdout, target="y", feature_names=["w", "z"], settings=strict)


def test_no_training_rows_left_is_a_leakage_error(plain_settings) -> None:
    holdout = [_rec(10, 0, x=1.0, y=0)]
    with pytest.raises(DataLeakageError, match="no training rows"):
        check_leakage(
            [_rec(1, 5, x=2.0, y=1)], holdout, target="y", feature_names=["x"],
            settings=plain_settings,
        )


# ---- through the pipeline --------------------------------------------------------------------
def test_retrain_job_reports_metrics_decision_output_and_leakage(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-d", name="cell_d"
    )
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-d", event_id="evt-stage-d"), tmp_path,
    )
    assert job.status == JobStatus.COMPLETED, job.error
    result = job.result
    assert result["outcome"] == "REGISTERED"

    live_eval = next(e for e in result["version_evaluations"] if e["is_live"])
    assert live_eval["metric_name"] == "accuracy"
    assert {"accuracy", "precision", "recall", "f1"} <= set(live_eval["metrics"])
    assert result["reuse_decision"]["thresholds"]["metric"] == "accuracy"

    decision = result["decision"]
    for field in (*_OUTPUT_FIELDS, "metrics"):
        assert decision[field], field
    assert decision["evidence"]["task_type"] == "classification"
    assert decision["fallback_strategy"] is not None
    assert "f1" in result["validation"]["candidate_metrics"]

    leakage = result["leakage"]
    assert leakage["checked"] and leakage["train_holdout_disjoint"]
    assert leakage["holdout_rows"] == result["validation"]["n_validation_rows"]
    assert leakage["train_end"] <= leakage["holdout_start"]
    with session_scope(session_factory) as s:
        started = s.query(AuditLog).filter_by(job_id=job.job_id, action="VALIDATION_STARTED").one()
        assert started.detail["leakage"]["train_rows_out"] == leakage["train_rows_out"]
        made = s.query(AuditLog).filter_by(
            job_id=job.job_id, action="ADAPTATION_DECISION_CREATED"
        ).one()
        assert made.detail["expected_cost"]["level"] in ("LOW", "HIGH")


def test_model_that_reads_its_own_target_is_never_retrained(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    def with_copy(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.assign(label_copy=frame[TARGET])

    historical = with_copy(_rsrp_regime(120, seed=20))
    leaky = LogisticRegression(max_iter=1000).fit(
        historical[[*FEATURES, "label_copy"]], historical[TARGET]
    )
    _seed(
        session_factory, registry, migrated_settings, model_id="cell-leak", name="cell_leak",
        models=[leaky], live="1", historical=historical,
        drifted=with_copy(_rsrp_regime(100, seed=21, prb_lo=5.0, prb_hi=6.0)),
    )
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-leak", event_id="evt-leak"), tmp_path,
    )
    assert job.status == JobStatus.FAILED
    assert "DATA_LEAKAGE" in json.dumps(job.error)
    assert registry.get_version_by_alias("cell_leak", migrated_settings.live_alias) == "1"
    assert [str(v.version) for v in registry.list_versions("cell_leak")] == ["1"]
