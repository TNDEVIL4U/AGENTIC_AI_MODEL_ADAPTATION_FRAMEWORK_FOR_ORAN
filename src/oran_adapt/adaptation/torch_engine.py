"""Member 3 - PyTorch engine: real gradient-descent training for torch.nn.Module models.
TORCH_FINE_TUNE continues training the current module in place from its existing weights (a
warm start - never a fresh reinitialization). TORCH_FULL_RETRAIN builds a fresh module with the
same architecture but reinitialized parameters and trains it from scratch, mirroring how
full_retrain() clones (never mutates) a sklearn estimator."""

from __future__ import annotations

import copy
import os

import joblib
import pandas as pd
import torch
from torch import nn

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import ArtifactError


def _to_tensors(X: pd.DataFrame, y: pd.Series, estimator_type: str) -> tuple[torch.Tensor, torch.Tensor]:
    X_t = torch.tensor(X.to_numpy(), dtype=torch.float32)
    if estimator_type == "classifier":
        y_t = torch.tensor(y.to_numpy(), dtype=torch.long)
    else:
        y_t = torch.tensor(y.to_numpy(), dtype=torch.float32).unsqueeze(-1)
    return X_t, y_t


def _loss_fn(estimator_type: str) -> nn.Module:
    return nn.CrossEntropyLoss() if estimator_type == "classifier" else nn.MSELoss()


def _run_training(
    model: nn.Module, X_t: torch.Tensor, y_t: torch.Tensor, *, estimator_type: str, epochs: int, lr: float
) -> None:
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = _loss_fn(estimator_type)
    model.train()
    for _ in range(epochs):
        optimizer.zero_grad()
        loss = loss_fn(model(X_t), y_t)
        loss.backward()
        optimizer.step()
    model.eval()


def _compute_metrics(
    model: nn.Module, X_t: torch.Tensor, y_t: torch.Tensor, estimator_type: str
) -> dict[str, float]:
    with torch.no_grad():
        outputs = model(X_t)
        if estimator_type == "classifier":
            accuracy = (outputs.argmax(dim=-1) == y_t).float().mean().item()
            return {"accuracy": float(accuracy)}
        rmse = torch.sqrt(nn.functional.mse_loss(outputs, y_t)).item()
        return {"rmse": float(rmse)}


def _save(model: nn.Module, artifact_dir: str) -> str:
    os.makedirs(artifact_dir, exist_ok=True)
    artifact_path = os.path.join(artifact_dir, "model.joblib")
    joblib.dump(model, artifact_path)
    return artifact_path


def _reset_parameters(model: nn.Module) -> None:
    for module in model.modules():
        reset = getattr(module, "reset_parameters", None)
        if callable(reset):
            reset()


def fine_tune_torch(
    current_model: nn.Module,
    *,
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    target_column: str,
    estimator_type: str,
    artifact_dir: str,
    epochs: int = 5,
    lr: float = 1e-2,
) -> CandidateModel:
    X_t, y_t = _to_tensors(X, y, estimator_type)
    try:
        _run_training(current_model, X_t, y_t, estimator_type=estimator_type, epochs=epochs, lr=lr)
    except Exception as exc:
        raise ArtifactError(
            f"torch fine-tuning failed on {type(current_model).__name__}", cause=str(exc)
        ) from exc

    metrics = _compute_metrics(current_model, X_t, y_t, estimator_type)
    artifact_path = _save(current_model, artifact_dir)

    return CandidateModel(
        engine=EngineKind.TORCH_FINE_TUNE,
        framework="torch",
        model_class=type(current_model).__name__,
        artifact_path=artifact_path,
        metrics=metrics,
        n_train_rows=len(X),
        feature_names=feature_names,
        target_column=target_column,
    )


def full_retrain_torch(
    current_model: nn.Module,
    *,
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    target_column: str,
    estimator_type: str,
    artifact_dir: str,
    epochs: int = 30,
    lr: float = 1e-2,
) -> CandidateModel:
    fresh = copy.deepcopy(current_model)
    _reset_parameters(fresh)

    X_t, y_t = _to_tensors(X, y, estimator_type)
    try:
        _run_training(fresh, X_t, y_t, estimator_type=estimator_type, epochs=epochs, lr=lr)
    except Exception as exc:
        raise ArtifactError(
            f"torch full retrain failed to fit {type(current_model).__name__}", cause=str(exc)
        ) from exc

    metrics = _compute_metrics(fresh, X_t, y_t, estimator_type)
    artifact_path = _save(fresh, artifact_dir)

    return CandidateModel(
        engine=EngineKind.TORCH_FULL_RETRAIN,
        framework="torch",
        model_class=type(fresh).__name__,
        artifact_path=artifact_path,
        metrics=metrics,
        n_train_rows=len(X),
        feature_names=feature_names,
        target_column=target_column,
    )
