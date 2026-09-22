"""Phase 8: Member 4 (validation) - V_current vs candidate on the same held-out set, real fitted
models throughout (no mocks), proving both the pass path and the fail path."""

from __future__ import annotations

import os

import joblib
import numpy as np
import pandas as pd
import pytest
import torch
from sklearn.linear_model import LinearRegression, LogisticRegression
from torch import nn

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import ValidationFailedError
from oran_adapt.validation.engine import validate_candidate

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"


def _clf_frame(n: int = 200, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0, 1, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    label = (prb > 0.5).astype(int)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp}), pd.Series(label, name=TARGET)


def _reg_frame(n: int = 200, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0, 1, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    target = 3.0 * prb - 0.01 * rsrp + rng.normal(0, 0.01, size=n)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp}), pd.Series(target, name=TARGET)


def _dump_candidate(
    model: object, tmp_path, *, framework: str = "sklearn", engine: EngineKind = EngineKind.SKLEARN_FULL_RETRAIN
) -> CandidateModel:
    artifact_dir = tmp_path / "candidate"
    os.makedirs(artifact_dir, exist_ok=True)
    artifact_path = os.path.join(artifact_dir, "model.joblib")
    joblib.dump(model, artifact_path)
    return CandidateModel(
        engine=engine,
        framework=framework,
        model_class=type(model).__name__,
        artifact_path=artifact_path,
        metrics={},
        n_train_rows=100,
        feature_names=FEATURES,
        target_column=TARGET,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class _AlwaysZero:
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.zeros(len(X), dtype=int)


class _Constant:
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.full(len(X), 999.0)


class _Broken:
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raise RuntimeError("boom")


# ---- classifier: pass / fail ----------------------------------------------------------------
def test_validate_candidate_classifier_pass(tmp_path, settings: Settings) -> None:
    X, y = _clf_frame()
    current = LogisticRegression().fit(X, y)
    candidate_model = LogisticRegression().fit(X, y)  # same quality as current
    candidate = _dump_candidate(candidate_model, tmp_path)

    report = validate_candidate(
        candidate,
        current,
        model_id="ran-kpi-v1",
        X=X,
        y=y,
        current_framework="sklearn",
        estimator_type="classifier",
        settings=settings,
    )

    assert report.passed is True
    assert report.metric_name == "accuracy"
    assert report.n_validation_rows == len(X)
    assert "current" in report.reason and "candidate" in report.reason


def test_validate_candidate_classifier_fail(tmp_path, settings: Settings) -> None:
    X, y = _clf_frame()
    current = LogisticRegression().fit(X, y)
    candidate = _dump_candidate(_AlwaysZero(), tmp_path)

    report = validate_candidate(
        candidate,
        current,
        model_id="ran-kpi-v1",
        X=X,
        y=y,
        current_framework="sklearn",
        estimator_type="classifier",
        settings=settings,
    )

    assert report.passed is False
    assert report.candidate_value < report.current_value


# ---- regressor: pass / fail ------------------------------------------------------------------
def test_validate_candidate_regressor_pass(tmp_path, settings: Settings) -> None:
    X, y = _reg_frame()
    current = LinearRegression().fit(X, y)
    candidate_model = LinearRegression().fit(X, y)
    candidate = _dump_candidate(candidate_model, tmp_path)

    report = validate_candidate(
        candidate,
        current,
        model_id="ran-kpi-v1",
        X=X,
        y=y,
        current_framework="sklearn",
        estimator_type="regressor",
        settings=settings,
    )

    assert report.passed is True
    assert report.metric_name == "rmse"


def test_validate_candidate_regressor_fail(tmp_path, settings: Settings) -> None:
    X, y = _reg_frame()
    current = LinearRegression().fit(X, y)
    candidate = _dump_candidate(_Constant(), tmp_path)

    report = validate_candidate(
        candidate,
        current,
        model_id="ran-kpi-v1",
        X=X,
        y=y,
        current_framework="sklearn",
        estimator_type="regressor",
        settings=settings,
    )

    assert report.passed is False
    assert report.candidate_value > report.current_value


# ---- torch framework, evaluated through the same gate -------------------------------------------
class _TinyClassifier(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.linear = nn.Linear(2, 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(x)


def test_validate_candidate_torch_classifier(tmp_path, settings: Settings) -> None:
    X, y = _clf_frame()
    torch.manual_seed(0)
    current = _TinyClassifier()
    candidate_model = _TinyClassifier()
    candidate_model.load_state_dict(current.state_dict())  # identical weights -> identical score
    candidate = _dump_candidate(
        candidate_model, tmp_path, framework="torch", engine=EngineKind.TORCH_FULL_RETRAIN
    )

    report = validate_candidate(
        candidate,
        current,
        model_id="ran-kpi-v1",
        X=X,
        y=y,
        current_framework="torch",
        estimator_type="classifier",
        settings=settings,
    )

    assert report.passed is True
    assert report.candidate_value == pytest.approx(report.current_value)


# ---- guard rails --------------------------------------------------------------------------------
def test_validate_candidate_raises_when_too_few_validation_rows(tmp_path, settings: Settings) -> None:
    X, y = _clf_frame(n=2)
    current = LogisticRegression().fit(*_clf_frame())
    candidate = _dump_candidate(LogisticRegression().fit(*_clf_frame()), tmp_path)

    with pytest.raises(ValidationFailedError):
        validate_candidate(
            candidate,
            current,
            model_id="ran-kpi-v1",
            X=X,
            y=y,
            current_framework="sklearn",
            estimator_type="classifier",
            settings=settings,
        )


def test_validate_candidate_raises_when_candidate_cannot_predict(tmp_path, settings: Settings) -> None:
    X, y = _clf_frame()
    current = LogisticRegression().fit(X, y)
    candidate = _dump_candidate(_Broken(), tmp_path)

    with pytest.raises(ValidationFailedError):
        validate_candidate(
            candidate,
            current,
            model_id="ran-kpi-v1",
            X=X,
            y=y,
            current_framework="sklearn",
            estimator_type="classifier",
            settings=settings,
        )
