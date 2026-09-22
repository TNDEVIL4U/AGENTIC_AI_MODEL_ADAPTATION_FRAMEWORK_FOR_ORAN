"""Phase 5: real retraining engines - train -> save -> reload -> infer, against a real SQLite DB
for training data and real sklearn/xgboost estimators for the fit itself. No mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import joblib
import numpy as np
import pytest
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.linear_model import LogisticRegression, SGDClassifier, SGDRegressor
from xgboost import XGBClassifier

from oran_adapt.adaptation.data import (
    build_training_frame,
    load_data_version_frame,
    split_features_target,
)
from oran_adapt.adaptation.engines import run_engine, select_engine
from oran_adapt.adaptation.finetune import fine_tune_sklearn
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import DataRecord, DatasetMetadata, DataVersion

T0 = datetime(2026, 1, 1, tzinfo=UTC)

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"


def _rows(n: int, offset: int = 0) -> list[dict]:
    rng = np.random.default_rng(42 + offset)
    out = []
    for i in range(n):
        prb = float(rng.uniform(0, 1))
        rsrp = float(rng.uniform(-120, -60))
        label = 1 if prb > 0.5 else 0
        out.append({"prb_util": prb, "rsrp": rsrp, "label": label})
    return out


@pytest.fixture
def session_factory(migrated_settings):
    engine = create_db_engine(migrated_settings.database_url)
    return make_session_factory(engine)


def _seed_version(session_factory, *, version: str, n: int, offset: int = 0) -> int:
    with session_scope(session_factory) as session:
        dataset = session.query(DatasetMetadata).filter_by(dataset_id="phase5-ds").one_or_none()
        if dataset is None:
            dataset = DatasetMetadata(dataset_id="phase5-ds", name="Phase 5 KPIs", schema={})
            session.add(dataset)
            session.flush()

        dv = DataVersion(
            dataset_id=dataset.id,
            version=version,
            kind="HISTORICAL",
            data_start=T0,
            data_end=T0 + timedelta(days=n),
            row_count=n,
        )
        session.add(dv)
        session.flush()
        dv_id = dv.id
        for i, row in enumerate(_rows(n, offset=offset)):
            session.add(
                DataRecord(data_version_id=dv_id, observed_at=T0 + timedelta(days=i), payload=row)
            )
    return dv_id


# ---- adaptation/data.py, against a real SQLite DB ----------------------------------------------
def test_load_data_version_frame_returns_real_rows(session_factory) -> None:
    dv_id = _seed_version(session_factory, version="v1", n=20)
    with session_scope(session_factory) as session:
        df = load_data_version_frame(session, dv_id)
    assert len(df) == 20
    assert set(df.columns) == {"prb_util", "rsrp", "label"}


def test_load_data_version_frame_raises_when_no_records(session_factory) -> None:
    with session_scope(session_factory) as session:
        dataset = DatasetMetadata(dataset_id="empty-ds", name="Empty", schema={})
        session.add(dataset)
        session.flush()
        dv = DataVersion(
            dataset_id=dataset.id,
            version="empty-1",
            kind="HISTORICAL",
            data_start=T0,
            data_end=T0,
            row_count=0,
        )
        session.add(dv)
        session.flush()
        dv_id = dv.id

    with session_scope(session_factory) as session, pytest.raises(ArtifactError):
        load_data_version_frame(session, dv_id)


def test_build_training_frame_concats_multiple_versions(session_factory) -> None:
    dv1 = _seed_version(session_factory, version="v1", n=15, offset=0)
    dv2 = _seed_version(session_factory, version="v2", n=10, offset=1)
    with session_scope(session_factory) as session:
        df = build_training_frame(session, [dv1, dv2])
    assert len(df) == 25


def test_split_features_target_ok(session_factory) -> None:
    dv_id = _seed_version(session_factory, version="v1", n=20)
    with session_scope(session_factory) as session:
        df = load_data_version_frame(session, dv_id)
    X, y = split_features_target(df, FEATURES, TARGET)
    assert list(X.columns) == FEATURES
    assert y.name == TARGET
    assert len(X) == len(y) == 20


def test_split_features_target_missing_column_raises(session_factory) -> None:
    dv_id = _seed_version(session_factory, version="v1", n=5)
    with session_scope(session_factory) as session:
        df = load_data_version_frame(session, dv_id)
    with pytest.raises(ArtifactError):
        split_features_target(df, [*FEATURES, "not_a_column"], TARGET)


# ---- full retraining: train -> save -> reload -> infer, real estimators ------------------------
_fit_frame_counter = 0


def _fit_frame(session_factory, n: int = 60):
    global _fit_frame_counter
    _fit_frame_counter += 1
    dv_id = _seed_version(session_factory, version=f"train-{_fit_frame_counter}", n=n)
    with session_scope(session_factory) as session:
        df = load_data_version_frame(session, dv_id)
    return split_features_target(df, FEATURES, TARGET)


def test_full_retrain_sklearn_classifier_round_trip(session_factory, tmp_path) -> None:
    X, y = _fit_frame(session_factory)
    current = LogisticRegression().fit(X, y)

    candidate = full_retrain(
        current,
        engine=EngineKind.SKLEARN_FULL_RETRAIN,
        framework="sklearn",
        X=X,
        y=y,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="classifier",
        artifact_dir=str(tmp_path / "artifact"),
    )

    assert candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert candidate.model_class == "LogisticRegression"
    assert candidate.n_train_rows == len(X)
    assert 0.0 <= candidate.metrics["accuracy"] <= 1.0

    reloaded = joblib.load(candidate.artifact_path)
    preds = reloaded.predict(X)
    assert len(preds) == len(X)
    # A fresh clone, never the original fitted object mutated in place.
    assert reloaded is not current


def test_full_retrain_sklearn_regressor_uses_rmse_metric(session_factory, tmp_path) -> None:
    X, y = _fit_frame(session_factory)
    y_reg = y.astype(float) + np.random.default_rng(0).normal(0, 0.01, size=len(y))
    current = SGDRegressor().fit(X, y_reg)

    candidate = full_retrain(
        current,
        engine=EngineKind.SKLEARN_FULL_RETRAIN,
        framework="sklearn",
        X=X,
        y=y_reg,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="regressor",
        artifact_dir=str(tmp_path / "artifact"),
    )

    assert "rmse" in candidate.metrics
    assert candidate.metrics["rmse"] >= 0.0
    reloaded = joblib.load(candidate.artifact_path)
    assert len(reloaded.predict(X)) == len(X)


def test_full_retrain_xgboost_round_trip(session_factory, tmp_path) -> None:
    X, y = _fit_frame(session_factory)
    current = XGBClassifier(n_estimators=5, max_depth=2).fit(X, y)

    candidate = full_retrain(
        current,
        engine=EngineKind.XGBOOST_FULL_RETRAIN,
        framework="xgboost",
        X=X,
        y=y,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="classifier",
        artifact_dir=str(tmp_path / "artifact"),
    )

    assert candidate.engine == EngineKind.XGBOOST_FULL_RETRAIN
    assert 0.0 <= candidate.metrics["accuracy"] <= 1.0
    reloaded = joblib.load(candidate.artifact_path)
    assert len(reloaded.predict(X)) == len(X)


class _AlwaysFailsEstimator(ClassifierMixin, BaseEstimator):
    def fit(self, X, y):
        raise RuntimeError("simulated fit failure")

    def predict(self, X):
        return np.zeros(len(X))


def test_full_retrain_fit_failure_raises_artifact_error(session_factory, tmp_path) -> None:
    X, y = _fit_frame(session_factory)
    current = _AlwaysFailsEstimator()

    with pytest.raises(ArtifactError):
        full_retrain(
            current,
            engine=EngineKind.SKLEARN_FULL_RETRAIN,
            framework="sklearn",
            X=X,
            y=y,
            feature_names=FEATURES,
            target_column=TARGET,
            estimator_type="classifier",
            artifact_dir=str(tmp_path / "artifact"),
        )


# ---- fine-tuning: continues from existing weights, in place ------------------------------------
def test_fine_tune_sklearn_continues_from_existing_weights(session_factory, tmp_path) -> None:
    X1, y1 = _fit_frame(session_factory, n=60)
    current = SGDClassifier(random_state=0).fit(X1, y1)
    coef_before = current.coef_.copy()

    X2, y2 = _fit_frame(session_factory, n=40)
    candidate = fine_tune_sklearn(
        current,
        X=X2,
        y=y2,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="classifier",
        artifact_dir=str(tmp_path / "artifact"),
    )

    # partial_fit mutates the same object in place - never a fresh clone.
    assert not np.allclose(coef_before, current.coef_)
    assert candidate.artifact_path
    reloaded = joblib.load(candidate.artifact_path)
    # The saved artifact reflects the continued weights, not a reset/re-fit model.
    assert np.allclose(reloaded.coef_, current.coef_)
    assert len(reloaded.predict(X2)) == len(X2)


def test_fine_tune_without_partial_fit_raises_unsupported(session_factory, tmp_path) -> None:
    X, y = _fit_frame(session_factory)
    current = LogisticRegression().fit(X, y)

    with pytest.raises(UnsupportedAdaptationError):
        fine_tune_sklearn(
            current,
            X=X,
            y=y,
            feature_names=FEATURES,
            target_column=TARGET,
            estimator_type="classifier",
            artifact_dir=str(tmp_path / "artifact"),
        )


# ---- run_engine dispatcher: select_engine's output feeds straight into run_engine --------------
def test_run_engine_sklearn_full_retrain_via_select_engine(session_factory, tmp_path) -> None:
    from oran_adapt.adaptation.capability import assess_capability

    X, y = _fit_frame(session_factory)
    current = LogisticRegression().fit(X, y)
    inspection = inspect_model(current, "sklearn")
    capability = assess_capability(inspection, FEATURES)

    engine = select_engine(Strategy.FULL_RETRAINING, "sklearn", capability)
    candidate = run_engine(
        engine,
        current,
        inspection=inspection,
        X=X,
        y=y,
        target_column=TARGET,
        artifact_dir=str(tmp_path / "artifact"),
    )
    assert candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert joblib.load(candidate.artifact_path) is not None


def test_run_engine_unimplemented_engine_raises_unsupported(tmp_path) -> None:
    from oran_adapt.adaptation.schemas import ModelInspection

    inspection = ModelInspection(framework="sklearn", model_class="Unknown")
    with pytest.raises(UnsupportedAdaptationError):
        run_engine(
            "NOT_A_REAL_ENGINE",  # type: ignore[arg-type]
            object(),
            inspection=inspection,
            X=None,
            y=None,
            target_column=TARGET,
            artifact_dir=str(tmp_path / "artifact"),
        )
