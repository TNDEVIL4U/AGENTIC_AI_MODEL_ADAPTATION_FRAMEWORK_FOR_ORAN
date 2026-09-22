"""Member 4 (validation) - scores a fitted model against a held-out validation set using the
framework-appropriate prediction call, producing the same metric names the adaptation engines
already report (accuracy for classifiers, rmse for regressors) so a candidate and V_current are
directly comparable."""

from __future__ import annotations

import pandas as pd
from sklearn.metrics import accuracy_score, mean_squared_error

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

    if estimator_type == "classifier":
        return {"accuracy": float(accuracy_score(y, predictions))}
    if estimator_type == "regressor":
        return {"rmse": float(mean_squared_error(y, predictions) ** 0.5)}
    raise ValidationFailedError(f"cannot score an estimator_type of {estimator_type!r}")
