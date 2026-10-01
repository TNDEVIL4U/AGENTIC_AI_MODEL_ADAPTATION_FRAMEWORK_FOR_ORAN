"""Member 3 - engine registry: maps an approved (strategy, framework, capability) combination to
the concrete engine that will carry it out, and dispatches to the real training code for every
engine (Phase 5: sklearn/xgboost full retrain, sklearn partial-fit fine-tuning; Phase 6: torch
fine-tune/full-retrain).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pandas as pd

from oran_adapt.adaptation.finetune import fine_tune_sklearn
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.adaptation.schemas import CandidateModel, CapabilityAssessment, ModelInspection
from oran_adapt.adaptation.torch_engine import fine_tune_torch, full_retrain_torch
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.core.errors import UnsupportedAdaptationError
from oran_adapt.core.frameworks import engine_for

if TYPE_CHECKING:
    from torch import nn

    from oran_adapt.core.config import Settings


@dataclass(frozen=True)
class TorchBudget:
    """Epochs and Adam step size for the torch engines (TORCH_* settings)."""

    fine_tune_epochs: int
    full_retrain_epochs: int
    learning_rate: float

    @classmethod
    def from_settings(cls, settings: Settings) -> TorchBudget:
        return cls(
            fine_tune_epochs=settings.torch_fine_tune_epochs,
            full_retrain_epochs=settings.torch_full_retrain_epochs,
            learning_rate=settings.torch_learning_rate,
        )

def select_engine(
    strategy: Strategy, framework: str, capability: CapabilityAssessment
) -> EngineKind:
    """The engine core.frameworks.ENGINES names for (strategy, framework), once the loaded
    artifact is known to support that kind of training."""
    if strategy == Strategy.FINE_TUNING:
        if not capability.supports_fine_tuning:
            raise UnsupportedAdaptationError(
                f"{framework} artifact has no fine-tuning capability: {capability.reason}"
            )
        kind = "fine-tuning"
    elif strategy == Strategy.FULL_RETRAINING:
        if not capability.supports_full_retraining:
            raise UnsupportedAdaptationError(
                f"{framework} artifact cannot be retrained: {capability.reason}"
            )
        kind = "full-retraining"
    else:
        raise UnsupportedAdaptationError(f"engine selection is not defined for strategy {strategy}")
    engine = engine_for(strategy, framework)
    if engine is None:
        raise UnsupportedAdaptationError(f"no {kind} engine for framework {framework!r}")
    return engine


def run_engine(
    engine: EngineKind,
    current_model: object,
    *,
    inspection: ModelInspection,
    X: pd.DataFrame,
    y: pd.Series,
    target_column: str,
    artifact_dir: str,
    torch_budget: TorchBudget,
) -> CandidateModel:
    """Carry out the engine chosen by select_engine() against the real current model and real
    training data, producing a saved candidate artifact ready for validation/registration.
    ``torch_budget`` sets the torch engines' epochs and learning rate."""
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
            cast("nn.Module", current_model),
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
            epochs=torch_budget.full_retrain_epochs,
            lr=torch_budget.learning_rate,
        )
    if engine == EngineKind.TORCH_FINE_TUNE:
        return fine_tune_torch(
            cast("nn.Module", current_model),
            X=X,
            y=y,
            feature_names=feature_names,
            target_column=target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=artifact_dir,
            epochs=torch_budget.fine_tune_epochs,
            lr=torch_budget.learning_rate,
        )
    raise AssertionError(f"unreachable: engine {engine} passed the support check above")
