"""Phase 6: real PyTorch fine-tuning - proves TORCH_FINE_TUNE warm-starts from the model's
existing weights (never resets them) while TORCH_FULL_RETRAIN trains a fresh, reinitialized
module of the same architecture without mutating the original. No mocks: real torch.nn.Module,
real backprop, real joblib round trip."""

from __future__ import annotations

import joblib
import numpy as np
import pandas as pd
import pytest
import torch
from torch import nn

from oran_adapt.adaptation.capability import assess_capability
from oran_adapt.adaptation.engines import run_engine, select_engine
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.torch_engine import fine_tune_torch, full_retrain_torch
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.core.errors import ArtifactError

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"


class _TinyClassifier(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.linear = nn.Linear(2, 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(x)


class _TinyRegressor(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.linear = nn.Linear(2, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(x)


def _frame(n: int, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0, 1, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    label = (prb > 0.5).astype(int)
    X = pd.DataFrame({"prb_util": prb, "rsrp": rsrp})
    y = pd.Series(label, name=TARGET)
    return X, y


def _reg_frame(n: int, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0, 1, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    target = prb * 2.0 + rsrp * 0.01
    X = pd.DataFrame({"prb_util": prb, "rsrp": rsrp})
    y = pd.Series(target, name=TARGET)
    return X, y


# ---- fine-tuning: continues from existing weights, in place ------------------------------------
def test_fine_tune_torch_continues_from_existing_weights(tmp_path) -> None:
    torch.manual_seed(0)
    model = _TinyClassifier()
    weight_before = model.linear.weight.detach().clone()

    X, y = _frame(200)
    candidate = fine_tune_torch(
        model,
        X=X,
        y=y,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="classifier",
        artifact_dir=str(tmp_path / "artifact"),
        epochs=20,
    )

    # partial training mutates the same module in place - never a fresh clone.
    assert not torch.allclose(weight_before, model.linear.weight)
    assert candidate.engine == EngineKind.TORCH_FINE_TUNE
    assert 0.0 <= candidate.metrics["accuracy"] <= 1.0

    reloaded = joblib.load(candidate.artifact_path)
    assert torch.allclose(reloaded.linear.weight, model.linear.weight)
    with torch.no_grad():
        preds = reloaded(torch.tensor(X.to_numpy(), dtype=torch.float32))
    assert preds.shape[0] == len(X)


def test_fine_tune_torch_regressor_uses_rmse_metric(tmp_path) -> None:
    torch.manual_seed(0)
    model = _TinyRegressor()
    X, y = _reg_frame(200)

    candidate = fine_tune_torch(
        model,
        X=X,
        y=y,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="regressor",
        artifact_dir=str(tmp_path / "artifact"),
        epochs=20,
    )

    assert "rmse" in candidate.metrics
    assert candidate.metrics["rmse"] >= 0.0


def test_fine_tune_torch_fit_failure_raises_artifact_error(tmp_path) -> None:
    model = _TinyClassifier()
    # 3 columns into a 2-in-feature module - forward() will raise a shape mismatch.
    X = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0], "c": [5.0, 6.0]})
    y = pd.Series([0, 1], name=TARGET)

    with pytest.raises(ArtifactError):
        fine_tune_torch(
            model,
            X=X,
            y=y,
            feature_names=list(X.columns),
            target_column=TARGET,
            estimator_type="classifier",
            artifact_dir=str(tmp_path / "artifact"),
        )


# ---- full retraining: fresh reinitialized weights, never mutates the original ------------------
def test_full_retrain_torch_reinitializes_and_does_not_mutate_original(tmp_path) -> None:
    torch.manual_seed(0)
    model = _TinyClassifier()
    weight_before = model.linear.weight.detach().clone()

    X, y = _frame(200)
    candidate = full_retrain_torch(
        model,
        X=X,
        y=y,
        feature_names=FEATURES,
        target_column=TARGET,
        estimator_type="classifier",
        artifact_dir=str(tmp_path / "artifact"),
        epochs=20,
    )

    # The original model object must be untouched.
    assert torch.allclose(weight_before, model.linear.weight)
    assert candidate.engine == EngineKind.TORCH_FULL_RETRAIN
    assert candidate.model_class == "_TinyClassifier"

    reloaded = joblib.load(candidate.artifact_path)
    assert reloaded is not model
    with torch.no_grad():
        preds = reloaded(torch.tensor(X.to_numpy(), dtype=torch.float32))
    assert preds.shape[0] == len(X)


# ---- run_engine dispatcher: select_engine's output feeds straight into run_engine --------------
def test_run_engine_torch_fine_tune_via_select_engine(tmp_path) -> None:
    torch.manual_seed(0)
    model = _TinyClassifier()
    inspection = inspect_model(model, "torch")
    capability = assess_capability(inspection, FEATURES)
    assert capability.supports_fine_tuning

    engine = select_engine(Strategy.FINE_TUNING, "torch", capability)
    X, y = _frame(100)
    candidate = run_engine(
        engine,
        model,
        inspection=inspection,
        X=X,
        y=y,
        target_column=TARGET,
        artifact_dir=str(tmp_path / "artifact"),
    )
    assert candidate.engine == EngineKind.TORCH_FINE_TUNE


def test_run_engine_torch_full_retrain_via_select_engine(tmp_path) -> None:
    torch.manual_seed(0)
    model = _TinyRegressor()
    inspection = inspect_model(model, "torch")
    capability = assess_capability(inspection, FEATURES)

    engine = select_engine(Strategy.FULL_RETRAINING, "torch", capability)
    X, y = _reg_frame(100)
    candidate = run_engine(
        engine,
        model,
        inspection=inspection,
        X=X,
        y=y,
        target_column=TARGET,
        artifact_dir=str(tmp_path / "artifact"),
    )
    assert candidate.engine == EngineKind.TORCH_FULL_RETRAIN


