"""Member 3 - fine-tuning engine: continue training a copy of the current model via
partial_fit(), rather than fitting a fresh estimator. Preserves whatever the model already
learned instead of discarding it, which is the whole point of fine-tuning over full retraining.
The live model is never trained in place: validation compares the candidate against it."""

from __future__ import annotations

import copy
import os
from typing import Protocol, cast

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, mean_squared_error

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError


class _IncrementalModel(Protocol):
    def partial_fit(self, X: pd.DataFrame, y: pd.Series) -> object: ...
    def predict(self, X: pd.DataFrame) -> object: ...


def fine_tune_sklearn(
    current_model: object,
    *,
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    target_column: str,
    estimator_type: str,
    artifact_dir: str,
) -> CandidateModel:
    if not hasattr(current_model, "partial_fit"):
        raise UnsupportedAdaptationError(
            f"{type(current_model).__name__} has no partial_fit - cannot fine-tune"
        )

    # partial_fit checked just above; the copy keeps the live model untouched.
    model = cast(_IncrementalModel, copy.deepcopy(current_model))
    try:
        model.partial_fit(X, y)
    except Exception as exc:
        raise ArtifactError(
            f"fine-tuning failed during partial_fit on {type(current_model).__name__}",
            cause=str(exc),
        ) from exc

    metrics: dict[str, float] = {}
    predictions = model.predict(X)
    if estimator_type == "classifier":
        metrics["accuracy"] = float(accuracy_score(y, predictions))
    elif estimator_type == "regressor":
        metrics["rmse"] = float(mean_squared_error(y, predictions) ** 0.5)

    os.makedirs(artifact_dir, exist_ok=True)
    artifact_path = os.path.join(artifact_dir, "model.joblib")
    joblib.dump(model, artifact_path)

    return CandidateModel(
        engine=EngineKind.SKLEARN_PARTIAL_FIT,
        framework="sklearn",
        model_class=type(current_model).__name__,
        artifact_path=artifact_path,
        metrics=metrics,
        n_train_rows=len(X),
        feature_names=feature_names,
        target_column=target_column,
    )
