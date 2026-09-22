"""Member 3 - full retraining engine: fit a fresh estimator with the current model's
hyperparameters (sklearn.base.clone - never the fitted state) on the merged historical+drifted
data, then save it to a local artifact path. Shared by SKLEARN_FULL_RETRAIN and
XGBOOST_FULL_RETRAIN - both are scikit-learn-API estimators."""

from __future__ import annotations

import os

import joblib
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import accuracy_score, mean_squared_error

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import ArtifactError


def full_retrain(
    current_model: object,
    *,
    engine: EngineKind,
    framework: str,
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    target_column: str,
    estimator_type: str,
    artifact_dir: str,
) -> CandidateModel:
    fresh = clone(current_model)
    try:
        fresh.fit(X, y)
    except Exception as exc:
        raise ArtifactError(
            f"full retrain failed to fit {type(current_model).__name__}", cause=str(exc)
        ) from exc

    metrics: dict[str, float] = {}
    predictions = fresh.predict(X)
    if estimator_type == "classifier":
        metrics["accuracy"] = float(accuracy_score(y, predictions))
    elif estimator_type == "regressor":
        metrics["rmse"] = float(mean_squared_error(y, predictions) ** 0.5)

    os.makedirs(artifact_dir, exist_ok=True)
    artifact_path = os.path.join(artifact_dir, "model.joblib")
    joblib.dump(fresh, artifact_path)

    return CandidateModel(
        engine=engine,
        framework=framework,
        model_class=type(fresh).__name__,
        artifact_path=artifact_path,
        metrics=metrics,
        n_train_rows=len(X),
        feature_names=feature_names,
        target_column=target_column,
    )
