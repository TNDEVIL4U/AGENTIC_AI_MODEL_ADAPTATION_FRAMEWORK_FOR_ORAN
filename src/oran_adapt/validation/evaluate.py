"""Member 4 (validation) - scores a fitted model against a held-out validation set using the
framework-appropriate prediction call, producing the same metric names the adaptation engines
already report (accuracy for classifiers, rmse for regressors) so a candidate and V_current are
directly comparable. The predictions come from the model's own framework; the primary accuracy
and RMSE scores are computed by Evidently AI, and the rest of the task's metric set (precision,
F1, ROC-AUC, MAE, R2, sMAPE, silhouette, ...) by validation.metrics."""

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

from oran_adapt.core.enums import TaskType
from oran_adapt.core.errors import ValidationFailedError
from oran_adapt.validation.metrics import compute_metrics, primary_metric, resolve_task

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


def _scores(model: object, X: pd.DataFrame, y: pd.Series, task: TaskType):
    """The score input the task's ranking metrics need, or None: the positive-class probability
    of a binary classifier, or an anomaly score (higher = more anomalous)."""
    if task == TaskType.CLASSIFICATION and hasattr(model, "predict_proba"):
        classes = list(getattr(model, "classes_", []))
        if len(classes) == 2 and set(pd.unique(y)) <= set(classes):
            return model.predict_proba(X)[:, 1]
    if task == TaskType.ANOMALY_DETECTION and hasattr(model, "decision_function"):
        return -np.asarray(model.decision_function(X))
    return None


def evaluate_model(
    model: object,
    X: pd.DataFrame,
    y: pd.Series | None,
    *,
    framework: str,
    estimator_type: str,
    task_type: str | None = None,
    y_train: pd.Series | None = None,
) -> dict[str, float]:
    """Score ``model`` on ``X``/``y`` with the metric set of its task (validation.metrics). The
    task's primary metric comes first - that is the one reuse and validation compare on;
    accuracy and RMSE are computed by Evidently. ``y`` may be None only for clustering.

    Raises ValidationFailedError if the model can't produce predictions at all (that's a hard
    validation failure, distinct from a merely worse score), if its task isn't scorable, or if
    the primary metric is undefined on this data.
    """
    try:
        task = resolve_task(task_type, estimator_type)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc
    try:
        if framework.lower() in _TORCH_LIKE:
            predictions = _predict_torch(model, X, estimator_type)
            score = None
        else:
            predictions = _predict_sklearn_like(model, X)
            score = _scores(model, X, y, task) if y is not None else None
    except Exception as exc:
        raise ValidationFailedError(
            f"model failed to produce predictions on the validation set: {type(model).__name__}",
            cause=str(exc),
        ) from exc

    scores = compute_metrics(task, y, predictions, y_score=score, X=X, y_train=y_train)
    primary = primary_metric(task)
    if task in (TaskType.CLASSIFICATION, TaskType.REGRESSION, TaskType.FORECASTING):
        # Evidently's value wins for the metric it computes (accuracy / rmse).
        scores = {**scores, **_evidently_score(y, predictions, estimator_type)}
    if primary not in scores:
        raise ValidationFailedError(
            f"{primary} is undefined for this {task.value.lower()} model on the given data",
            n_rows=len(X),
        )
    return {primary: scores[primary], **{k: v for k, v in scores.items() if k != primary}}
