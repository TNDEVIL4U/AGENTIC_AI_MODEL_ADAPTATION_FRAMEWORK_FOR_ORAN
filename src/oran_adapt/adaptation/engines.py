"""Member 3 - engine registry: maps an approved (strategy, framework, capability) combination to
the concrete engine that will carry it out, and dispatches to the real training code for every
engine (Phase 5: sklearn/xgboost full retrain, sklearn partial-fit fine-tuning; Phase 6: torch
fine-tune/full-retrain).
"""

from __future__ import annotations

import pandas as pd

from oran_adapt.adaptation.finetune import fine_tune_sklearn
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.adaptation.schemas import CandidateModel, CapabilityAssessment, ModelInspection
from oran_adapt.adaptation.torch_engine import fine_tune_torch, full_retrain_torch
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.core.errors import UnsupportedAdaptationError

_TORCH_LIKE = {"torch", "pytorch"}


def select_engine(
    strategy: Strategy, framework: str, capability: CapabilityAssessment
) -> EngineKind:
    fw = framework.lower()

    if strategy == Strategy.FINE_TUNING:
        if not capability.supports_fine_tuning:
            raise UnsupportedAdaptationError(
                f"{framework} artifact has no fine-tuning capability: {capability.reason}"
            )
        if fw == "sklearn":
            return EngineKind.SKLEARN_PARTIAL_FIT
        if fw in _TORCH_LIKE:
            return EngineKind.TORCH_FINE_TUNE
        raise UnsupportedAdaptationError(f"no fine-tuning engine for framework {framework!r}")

    if strategy == Strategy.FULL_RETRAINING:
        if not capability.supports_full_retraining:
            raise UnsupportedAdaptationError(
                f"{framework} artifact cannot be retrained: {capability.reason}"
            )
        if fw == "sklearn":
            return EngineKind.SKLEARN_FULL_RETRAIN
        if fw == "xgboost":
            return EngineKind.XGBOOST_FULL_RETRAIN
        if fw in _TORCH_LIKE:
            return EngineKind.TORCH_FULL_RETRAIN
        raise UnsupportedAdaptationError(f"no full-retraining engine for framework {framework!r}")

    raise UnsupportedAdaptationError(f"engine selection is not defined for strategy {strategy}")


def run_engine(
    engine: EngineKind,
    current_model: object,
    *,
    inspection: ModelInspection,
    X: pd.DataFrame,
    y: pd.Series,
    target_column: str,
    artifact_dir: str,
    torch_fine_tune_epochs: int | None = None,
    torch_full_retrain_epochs: int | None = None,
    torch_learning_rate: float | None = None,
) -> CandidateModel:
    """Carry out the engine chosen by select_engine() against the real current model and real
    training data, producing a saved candidate artifact ready for validation/registration.
    The torch epoch budgets and learning rate default to the engines' own defaults when not given."""
    if engine not in (
        EngineKind.SKLEARN_FULL_RETRAIN,
        EngineKind.XGBOOST_FULL_RETRAIN,
        EngineKind.SKLEARN_PARTIAL_FIT,
        EngineKind.TORCH_FULL_RETRAIN,
        EngineKind.TORCH_FINE_TUNE,
    ):
        raise UnsupportedAdaptationError(f"engine {engine} is not implemented yet")

    feature_names = list(X.columns)

    if engine == EngineKind.SKLEARN_FULL_RETRAIN:
        return full_retrain(
            current_model,
            engine=engine,
            framework="sklearn",
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
        )
    if engine == EngineKind.XGBOOST_FULL_RETRAIN:
        return full_retrain(
            current_model,
            engine=engine,
            framework="xgboost",
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
        )
    if engine == EngineKind.SKLEARN_PARTIAL_FIT:
        return fine_tune_sklearn(
            current_model,
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
        )
    if engine == EngineKind.TORCH_FULL_RETRAIN:
        return full_retrain_torch(
            current_model,
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
            **({"epochs": torch_full_retrain_epochs} if torch_full_retrain_epochs else {}),
            **({"lr": torch_learning_rate} if torch_learning_rate else {}),
        )
    if engine == EngineKind.TORCH_FINE_TUNE:
        return fine_tune_torch(
            current_model,
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
            **({"epochs": torch_fine_tune_epochs} if torch_fine_tune_epochs else {}),
            **({"lr": torch_learning_rate} if torch_learning_rate else {}),
        )
    raise AssertionError(f"unreachable: engine {engine} passed the support check above")
