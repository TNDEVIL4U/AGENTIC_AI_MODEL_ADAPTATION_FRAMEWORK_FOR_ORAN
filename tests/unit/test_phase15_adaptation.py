"""Phase F (Rule 7): Member 3 inspects the real artifact (framework, task, inputs, preprocessing,
classes) and fine-tunes only when the artifact technically supports it with a native engine or
the LLM adapter; otherwise it carries the adaptation out as a native full retrain and says so,
instead of failing the job."""

from __future__ import annotations

import numpy as np
import pandas as pd
import torch
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LogisticRegression, SGDRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from torch import nn
from xgboost import XGBRegressor

from oran_adapt.adaptation.capability import assess_capability, assess_schema_compatibility
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.orchestrator.pipeline import _produce_candidate

FEATURES = ["prb_util", "rsrp"]
TARGET = "throughput"
SETTINGS = Settings(_env_file=None)


def _frame(n: int = 60) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"prb_util": rng.uniform(0, 100, n), "rsrp": rng.uniform(-120, -70, n)})
    return X, pd.Series(0.5 * X["prb_util"] - 0.1 * X["rsrp"], name=TARGET)


def _labels(X: pd.DataFrame) -> pd.Series:
    return pd.Series((X["rsrp"] > -95).astype(int), name=TARGET)


def _produce(strategy: Strategy, model: object, framework: str, X, y, tmp_path):
    return _produce_candidate(
        strategy,
        model,
        inspection=inspect_model(model, framework),
        framework=framework,
        X=X,
        y=y,
        target_column=TARGET,
        settings=SETTINGS,
        llm_client=None,
        workdir=str(tmp_path),
    )


# ---- fine-tune only when technically supported, otherwise full retraining ----------------------
def test_fine_tuning_uses_the_native_engine_when_the_artifact_supports_it(tmp_path):
    X, y = _frame()
    candidate = _produce(Strategy.FINE_TUNING, SGDRegressor().fit(X, y), "sklearn", X, y, tmp_path)

    assert candidate.engine == EngineKind.SKLEARN_PARTIAL_FIT
    assert candidate.applied_strategy == Strategy.FINE_TUNING
    assert candidate.adaptation_note == ""


def test_fine_tuning_an_artifact_without_any_mechanism_becomes_full_retraining(tmp_path):
    X, y = _frame()
    model = RandomForestRegressor(n_estimators=5, random_state=0).fit(X, y)
    candidate = _produce(Strategy.FINE_TUNING, model, "sklearn", X, y, tmp_path)

    assert candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert candidate.applied_strategy == Strategy.FULL_RETRAINING
    assert "RandomForestRegressor" in candidate.adaptation_note


def test_fine_tuning_xgboost_becomes_its_native_full_retrain(tmp_path):
    X, y = _frame()
    model = XGBRegressor(n_estimators=5, max_depth=2).fit(X, y)
    candidate = _produce(Strategy.FINE_TUNING, model, "xgboost", X, y, tmp_path)

    assert candidate.engine == EngineKind.XGBOOST_FULL_RETRAIN
    assert candidate.applied_strategy == Strategy.FULL_RETRAINING


def test_warm_start_only_fine_tuning_without_an_llm_becomes_full_retraining(tmp_path):
    # warm_start is a fine-tuning mechanism the LLM adapter can use, but no native engine uses
    # it; with no LLM configured the job still adapts, as a native full retrain.
    X, _ = _frame()
    y = _labels(X)
    model = LogisticRegression(warm_start=True).fit(X, y)
    candidate = _produce(Strategy.FINE_TUNING, model, "sklearn", X, y, tmp_path)

    assert candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert candidate.applied_strategy == Strategy.FULL_RETRAINING
    assert "no native fine-tuning engine" in candidate.adaptation_note


def test_full_retraining_is_left_as_requested(tmp_path):
    X, y = _frame()
    candidate = _produce(
        Strategy.FULL_RETRAINING, SGDRegressor().fit(X, y), "sklearn", X, y, tmp_path
    )
    assert candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert candidate.applied_strategy == Strategy.FULL_RETRAINING


# ---- inspection of the real artifact --------------------------------------------------------------
def test_inspection_reports_preprocessing_and_classes():
    X, _ = _frame()
    y = _labels(X)
    pipe = make_pipeline(StandardScaler(), LogisticRegression()).fit(X, y)
    inspection = inspect_model(pipe, "sklearn")

    assert inspection.estimator_type == "classifier"
    assert inspection.preprocessing_steps == ["StandardScaler"]
    assert inspection.classes == ["0", "1"]
    assert inspection.feature_names_in == FEATURES


def test_torch_input_width_must_match_the_features():
    inspection = inspect_model(nn.Sequential(nn.Linear(3, 1)), "torch")

    mismatch = assess_schema_compatibility(inspection, FEATURES)
    assert mismatch.compatible is False
    assert "expects 3 input features" in mismatch.reason
    assert assess_capability(inspection, FEATURES).supports_full_retraining is False

    torch.manual_seed(0)
    ok = inspect_model(nn.Sequential(nn.Linear(2, 1)), "torch")
    assert assess_schema_compatibility(ok, FEATURES).compatible is True
