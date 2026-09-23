"""Member 4 (validation) - scores a fitted model against a held-out validation set using the
framework-appropriate prediction call, producing the same metric names the adaptation engines
already report (accuracy for classifiers, rmse for regressors) so a candidate and V_current are
directly comparable. The predictions come from the model's own framework; the scores are
computed by Evidently AI's Accuracy and RMSE metrics."""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from evidently import (
    BinaryClassification,
    DataDefinition,
    Dataset,
    MulticlassClassification,
    Regression,
    Report,
)
from evidently.metrics import RMSE, Accuracy

from oran_adapt.core.errors import ValidationFailedError

_TORCH_LIKE = {"torch", "pytorch"}


def _predict_sklearn_like(model: object, X: pd.DataFrame):
    return model.predict(X)


def _predict_torch(model: object, X: pd.DataFrame, estimator_type: str):
    import torch

    model.eval()
    with torch.no_grad():
        outputs = model(torch.tensor(X.to_numpy(), dtype=torch.float32))
    if estimator_type == "classifier":
        return outputs.argmax(dim=-1).numpy()
    return outputs.squeeze(-1).numpy()


def _evidently_score(y: pd.Series, predictions, estimator_type: str) -> dict[str, float]:
    frame = pd.DataFrame({"target": np.asarray(y), "prediction": np.asarray(predictions)})
    if estimator_type == "classifier":
        metric, name = Accuracy(), "accuracy"
        labels = pd.unique(frame[["target", "prediction"]].to_numpy().ravel())
        if len(labels) <= 2:
            # Evidently treats two classes as binary and needs a positive label that exists;
            # which one is picked doesn't change accuracy.
            task = BinaryClassification(
                target="target", prediction_labels="prediction", pos_label=labels[-1]
            )
        else:
            task = MulticlassClassification(target="target", prediction_labels="prediction")
        definition = DataDefinition(classification=[task])
    else:
        metric, name = RMSE(), "rmse"
        definition = DataDefinition(regression=[Regression(target="target", prediction="prediction")])
    with warnings.catch_warnings():
        # Evidently also computes per-label precision internally; its "ill-defined" warnings
        # for labels never predicted don't affect accuracy.
        warnings.simplefilter("ignore")
        snapshot = Report([metric]).run(Dataset.from_pandas(frame, data_definition=definition), None)
    return {name: float(snapshot.dict()["metrics"][0]["value"])}


def evaluate_model(
    model: object, X: pd.DataFrame, y: pd.Series, *, framework: str, estimator_type: str
) -> dict[str, float]:
    """Raises ValidationFailedError if the model can't produce predictions at all (that's a hard
    validation failure, distinct from a merely worse score) or if estimator_type isn't scorable.
    """
    try:
        if framework.lower() in _TORCH_LIKE:
            predictions = _predict_torch(model, X, estimator_type)
        else:
            predictions = _predict_sklearn_like(model, X)
    except Exception as exc:
        raise ValidationFailedError(
            f"model failed to produce predictions on the validation set: {type(model).__name__}",
            cause=str(exc),
        ) from exc

    if estimator_type in ("classifier", "regressor"):
        return _evidently_score(y, predictions, estimator_type)
    raise ValidationFailedError(f"cannot score an estimator_type of {estimator_type!r}")
