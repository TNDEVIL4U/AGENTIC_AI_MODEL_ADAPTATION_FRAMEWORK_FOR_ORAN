"""Member 4 (validation) - task-agnostic metrics: which metrics a model is scored with depends on
its task (classification, regression, forecasting, clustering, anomaly detection), not on an
assumption that every model is a classifier.

A metric is reported only when it is defined for the data at hand: ROC-AUC/PR-AUC need scores
and both classes present, MAPE needs no zero targets, MASE needs a training series, clustering
scores need 2..n-1 clusters. An undefined metric is left out, never filled in with a guess.
Without labels there is no performance metric at all; ``prediction_shift`` then compares the
prediction distributions instead.
"""

from __future__ import annotations

import math

import numpy as np
from sklearn import metrics as skm

from oran_adapt.core.enums import TaskType

# The metric each task is compared on (reuse, validation). Classification and regression keep
# the names the engines, registry and existing records already use.
PRIMARY_METRIC: dict[TaskType, str] = {
    TaskType.CLASSIFICATION: "accuracy",
    TaskType.REGRESSION: "rmse",
    TaskType.FORECASTING: "rmse",
    TaskType.CLUSTERING: "silhouette",
    TaskType.ANOMALY_DETECTION: "f1",
}

_HIGHER_IS_BETTER = frozenset(
    {"accuracy", "precision", "recall", "f1", "roc_auc", "pr_auc", "r2", "silhouette"}
)
_LOWER_IS_BETTER = frozenset({"mae", "mse", "rmse", "mape", "smape", "mase", "davies_bouldin"})

_ALIASES: dict[str, TaskType] = {
    "classification": TaskType.CLASSIFICATION,
    "classifier": TaskType.CLASSIFICATION,
    "regression": TaskType.REGRESSION,
    "regressor": TaskType.REGRESSION,
    "forecasting": TaskType.FORECASTING,
    "forecast": TaskType.FORECASTING,
    "time_series": TaskType.FORECASTING,
    "timeseries": TaskType.FORECASTING,
    "clustering": TaskType.CLUSTERING,
    "clusterer": TaskType.CLUSTERING,
    "anomaly_detection": TaskType.ANOMALY_DETECTION,
    "anomaly": TaskType.ANOMALY_DETECTION,
    "outlier_detector": TaskType.ANOMALY_DETECTION,
    "outlier_detection": TaskType.ANOMALY_DETECTION,
}

# Which estimator types (from adaptation.inspector) can serve which task.
_ESTIMATOR_FOR_TASK: dict[TaskType, str] = {
    TaskType.CLASSIFICATION: "classifier",
    TaskType.REGRESSION: "regressor",
    TaskType.FORECASTING: "regressor",
    TaskType.CLUSTERING: "clusterer",
    TaskType.ANOMALY_DETECTION: "outlier_detector",
}


def higher_is_better(metric: str) -> bool:
    if metric in _HIGHER_IS_BETTER:
        return True
    if metric in _LOWER_IS_BETTER:
        return False
    raise ValueError(f"unknown metric {metric!r}")


def resolve_task(task_type: str | None, estimator_type: str) -> TaskType:
    """The recorded task_type wins when it agrees with what the artifact is (a forecaster is a
    regressor underneath); otherwise the inspected estimator type decides. Raises ValueError when
    neither names a scorable task."""
    declared = _ALIASES.get((task_type or "").strip().lower().replace("-", "_").replace(" ", "_"))
    if declared is not None and _ESTIMATOR_FOR_TASK[declared] == estimator_type:
        return declared
    inferred = _ALIASES.get(estimator_type)
    if inferred is None:
        raise ValueError(f"cannot score an estimator_type of {estimator_type!r}")
    return inferred


def primary_metric(task: TaskType) -> str:
    return PRIMARY_METRIC[task]


def _finite(values: dict[str, float]) -> dict[str, float]:
    return {k: float(v) for k, v in values.items() if v is not None and math.isfinite(float(v))}


def _classification(y_true: np.ndarray, y_pred: np.ndarray, y_score) -> dict[str, float]:
    labels = np.unique(np.concatenate([y_true, y_pred]))
    out = {"accuracy": skm.accuracy_score(y_true, y_pred)}
    if len(labels) <= 2:
        pos = labels[-1]
        kw = {"average": "binary", "pos_label": pos, "zero_division": 0}
    else:
        pos = None
        kw = {"average": "weighted", "zero_division": 0}
    out["precision"] = skm.precision_score(y_true, y_pred, **kw)
    out["recall"] = skm.recall_score(y_true, y_pred, **kw)
    out["f1"] = skm.f1_score(y_true, y_pred, **kw)
    # Ranking metrics only for binary targets with scores and both classes present.
    if y_score is not None and pos is not None and len(np.unique(y_true)) == 2:
        positive = (y_true == pos).astype(int)
        out["roc_auc"] = skm.roc_auc_score(positive, y_score)
        out["pr_auc"] = skm.average_precision_score(positive, y_score)
    return out


def _regression(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    mse = skm.mean_squared_error(y_true, y_pred)
    out = {"mae": skm.mean_absolute_error(y_true, y_pred), "mse": mse, "rmse": math.sqrt(mse)}
    if len(y_true) >= 2 and np.var(y_true) > 0:
        out["r2"] = skm.r2_score(y_true, y_pred)
    if not np.any(y_true == 0):
        out["mape"] = skm.mean_absolute_percentage_error(y_true, y_pred)
    return out


def _forecasting(y_true: np.ndarray, y_pred: np.ndarray, y_train, season: int) -> dict[str, float]:
    out = {k: v for k, v in _regression(y_true, y_pred).items() if k in ("mae", "rmse", "mape")}
    denom = np.abs(y_true) + np.abs(y_pred)
    nonzero = denom > 0
    if nonzero.any():
        out["smape"] = float(np.mean(2.0 * np.abs(y_pred - y_true)[nonzero] / denom[nonzero]))
    # MASE scales the error by an in-sample seasonal-naive forecast of the training series.
    if y_train is not None:
        train = np.asarray(y_train, dtype=float)
        if len(train) > season:
            naive = np.mean(np.abs(train[season:] - train[:-season]))
            if naive > 0:
                out["mase"] = out["mae"] / naive
    return out


def _clustering(X, labels: np.ndarray) -> dict[str, float]:
    if X is None:
        return {}
    n_clusters = len(np.unique(labels))
    if not 2 <= n_clusters <= len(labels) - 1:
        return {}
    data = np.asarray(X, dtype=float)
    return {
        "silhouette": skm.silhouette_score(data, labels),
        "davies_bouldin": skm.davies_bouldin_score(data, labels),
    }


def anomaly_flags(values) -> np.ndarray:
    """True where a row is an anomaly. sklearn outlier detectors predict -1 (outlier) / 1
    (inlier); labelled data usually marks anomalies 1 (or True) and normal rows 0."""
    arr = np.asarray(values)
    if arr.dtype == bool:
        return arr
    uniq = set(np.unique(arr).tolist())
    if uniq <= {-1, 1}:
        return arr == -1
    return arr == 1


def _anomaly(y_true: np.ndarray, y_pred: np.ndarray, y_score) -> dict[str, float]:
    truth, pred = anomaly_flags(y_true), anomaly_flags(y_pred)
    out = {
        "precision": skm.precision_score(truth, pred, zero_division=0),
        "recall": skm.recall_score(truth, pred, zero_division=0),
        "f1": skm.f1_score(truth, pred, zero_division=0),
    }
    if y_score is not None and 0 < truth.sum() < len(truth):
        out["pr_auc"] = skm.average_precision_score(truth, y_score)
    return out


def compute_metrics(
    task: TaskType,
    y_true,
    y_pred,
    *,
    y_score=None,
    X=None,
    y_train=None,
    season: int = 1,
) -> dict[str, float]:
    """Score predictions for ``task``. The task's primary metric comes first when it is defined.
    Supervised tasks need ``y_true``; without it they get no performance metric (use
    ``prediction_shift``). ``y_score`` is the positive-class probability (classification) or
    an anomaly score where higher means more anomalous."""
    pred = np.asarray(y_pred)
    if task == TaskType.CLUSTERING:
        out = _clustering(X, pred)
    elif y_true is None:
        return {}
    else:
        truth = np.asarray(y_true)
        if task == TaskType.CLASSIFICATION:
            out = _classification(truth, pred, y_score)
        elif task == TaskType.REGRESSION:
            out = _regression(truth.astype(float), pred.astype(float))
        elif task == TaskType.FORECASTING:
            out = _forecasting(truth.astype(float), pred.astype(float), y_train, season)
        else:
            out = _anomaly(truth, pred, y_score)
    out = _finite(out)
    primary = PRIMARY_METRIC[task]
    if primary in out:
        out = {primary: out[primary], **{k: v for k, v in out.items() if k != primary}}
    return out


def prediction_shift(reference, current) -> dict[str, float]:
    """Label-free comparison of two prediction distributions (e.g. LIVE's predictions on the
    training-era data vs on the current data). Numeric predictions get PSI and the KS p-value;
    categorical ones get the total variation distance between class frequencies."""
    ref, cur = np.asarray(reference), np.asarray(current)
    if len(ref) == 0 or len(cur) == 0:
        return {}
    numeric = np.issubdtype(ref.dtype, np.number) and np.issubdtype(cur.dtype, np.number)
    if numeric and len(np.unique(np.concatenate([ref, cur]))) > 2:
        from oran_adapt.analysis.comparison import _evidently_drift

        drift = _evidently_drift(
            {"prediction": ref.astype(float)}, {"prediction": cur.astype(float)}
        )
        return _finite(
            {
                "prediction_psi": drift[("prediction", "psi")],
                "prediction_ks_pvalue": drift[("prediction", "ks")],
                "prediction_mean_shift": float(np.mean(cur) - np.mean(ref)),
            }
        )
    labels = np.unique(np.concatenate([ref.astype(str), cur.astype(str)]))
    ref_freq = np.array([np.mean(ref.astype(str) == lab) for lab in labels])
    cur_freq = np.array([np.mean(cur.astype(str) == lab) for lab in labels])
    return {"prediction_tvd": float(0.5 * np.abs(ref_freq - cur_freq).sum())}
