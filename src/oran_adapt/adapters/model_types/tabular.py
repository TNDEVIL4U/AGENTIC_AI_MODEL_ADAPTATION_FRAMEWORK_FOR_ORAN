"""Model type plugins for tabular models: one row in, one prediction out.

* ``sklearn`` and ``xgboost`` - scikit-learn-API estimators, adapted by the built-in engines
  (core.frameworks.ENGINES): partial_fit fine-tuning, and a full retrain from the estimator's
  own hyperparameters.
* ``lightgbm`` and ``catboost`` - gradient-boosted trees behind the scikit-learn API. Full
  retraining refits from the hyperparameters; fine-tuning is continued boosting: the new trees
  are fitted on the new data on top of the current model (``init_model``). The libraries are
  never imported here - a model is recognised by the module its class comes from.
* ``torch`` - a ``torch.nn.Module`` that maps a row of features to an output (no recurrent,
  convolutional or attention layer and no ``sequence_window``: those belong to the
  ``torch-sequence`` plugin), adapted by the built-in torch engines.

Rows are independent for these models, so none of them is temporal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np

from oran_adapt.adaptation.engines import TorchBudget, run_engine
from oran_adapt.adaptation.inspector import inspect_sklearn_like, inspect_torch
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.core.enums import EngineKind, Strategy
from oran_adapt.core.errors import UnsupportedAdaptationError
from oran_adapt.core.frameworks import TORCH_FRAMEWORKS, engine_for
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import TrainingSet

_STRATEGIES = (Strategy.FINE_TUNING, Strategy.FULL_RETRAINING)


def _module_root(model: object) -> str:
    return type(model).__module__.split(".", 1)[0]


def _builtin_engines(framework: str) -> dict[Strategy, str]:
    return {s: e.value for s in _STRATEGIES if (e := engine_for(s, framework)) is not None}


class BuiltinEngineType:
    """A framework the built-in engines adapt (sklearn, xgboost, torch)."""

    def __init__(self, frameworks: frozenset[str], budget: TorchBudget) -> None:
        self._frameworks = frameworks
        self._engines = _builtin_engines(next(iter(sorted(frameworks))))
        self.budget = budget

    @property
    def frameworks(self) -> frozenset[str]:
        return self._frameworks

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return self._engines

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        engine = self._engines.get(strategy)
        if engine is None:
            raise UnsupportedAdaptationError(
                f"no {strategy.value} engine for framework {inspection.framework!r}"
            )
        return run_engine(
            EngineKind(engine),
            model,
            inspection=inspection,
            X=data.X,
            y=data.y,
            target_column=data.target_column,
            artifact_dir=data.artifact_dir,
            torch_budget=self.budget,
        )


class SklearnType(BuiltinEngineType):
    def accepts(self, model: object, framework: str) -> bool:
        from sklearn.base import BaseEstimator

        # Duck-typed estimators (the scikit-learn API without the base class) are sklearn too.
        return isinstance(model, BaseEstimator) or callable(getattr(model, "predict", None))

    def inspect(self, model: object, framework: str) -> ModelInspection:
        return inspect_sklearn_like(model, framework)

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        return np.asarray(model.predict(X))  # type: ignore[attr-defined]


class XgboostType(SklearnType):
    def accepts(self, model: object, framework: str) -> bool:
        return _module_root(model) == "xgboost" and hasattr(model, "predict")


class TorchTabularType(BuiltinEngineType):
    def accepts(self, model: object, framework: str) -> bool:
        from torch import nn

        from oran_adapt.adapters.model_types.sequence import is_torch_sequence

        return isinstance(model, nn.Module) and not is_torch_sequence(model)

    def inspect(self, model: object, framework: str) -> ModelInspection:
        return inspect_torch(model)

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        import torch

        module: Any = model
        module.eval()
        with torch.no_grad():
            outputs = module(torch.tensor(X.to_numpy(), dtype=torch.float32))
        if inspection.estimator_type == "classifier":
            return np.asarray(outputs.argmax(dim=-1).numpy())
        return np.asarray(outputs.squeeze(-1).numpy())


class BoostingType:
    """LightGBM or CatBoost behind the scikit-learn API. ``init_model`` is what the library's
    ``fit`` takes to continue boosting from: LightGBM's fitted booster (``booster_``),
    CatBoost's fitted model itself."""

    def __init__(self, framework: str, init_from_booster: bool) -> None:
        self.framework = framework
        self.init_from_booster = init_from_booster
        prefix = framework.upper()
        self._engines = {
            Strategy.FINE_TUNING: f"{prefix}_CONTINUED_BOOSTING",
            Strategy.FULL_RETRAINING: f"{prefix}_FULL_RETRAIN",
        }

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset({self.framework})

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return self._engines

    def accepts(self, model: object, framework: str) -> bool:
        return (
            _module_root(model) == self.framework
            and hasattr(model, "fit")
            and hasattr(model, "predict")
        )

    def inspect(self, model: object, framework: str) -> ModelInspection:
        inspection = inspect_sklearn_like(model, self.framework)
        # Boosting can always add trees to a fitted model.
        return inspection.model_copy(update={"supports_warm_start": True})

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        fit_kwargs: dict[str, object] = {}
        if strategy == Strategy.FINE_TUNING:
            init = getattr(model, "booster_", None) if self.init_from_booster else model
            if init is None:
                raise UnsupportedAdaptationError(
                    f"the {self.framework} model is not fitted; there is nothing to continue "
                    "boosting from"
                )
            fit_kwargs["init_model"] = init
        elif strategy != Strategy.FULL_RETRAINING:
            raise UnsupportedAdaptationError(f"no {strategy.value} engine for {self.framework}")
        return full_retrain(
            model,
            engine=self._engines[strategy],
            framework=self.framework,
            X=data.X,
            y=data.y,
            feature_names=list(data.X.columns),
            target_column=data.target_column,
            estimator_type=inspection.estimator_type,
            artifact_dir=data.artifact_dir,
            fit_kwargs=fit_kwargs,
        )

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        predictions = np.asarray(model.predict(X))  # type: ignore[attr-defined]
        return predictions.reshape(len(X), -1).squeeze(-1)


def _budget(settings: Settings) -> TorchBudget:
    return TorchBudget.from_settings(settings)


def _spec(name: str, description: str, factory: Any, *, keys: tuple[str, ...] = (),
          distributions: tuple[str, ...] = (), frameworks: frozenset[str]) -> AdapterSpec[Any]:
    return AdapterSpec(
        capability=Capability(
            port="model_type",
            adapter=name,
            description=description,
            features=frozenset({"tabular", *(f"framework:{f}" for f in frameworks)}),
            config_keys=keys,
            distributions=distributions,
        ),
        factory=factory,
    )


_TORCH_KEYS = ("torch_fine_tune_epochs", "torch_full_retrain_epochs", "torch_learning_rate")

SKLEARN = _spec(
    "sklearn",
    "scikit-learn estimators: partial_fit fine-tuning, full retraining",
    lambda s: SklearnType(frozenset({"sklearn"}), _budget(s)),
    frameworks=frozenset({"sklearn"}),
)
XGBOOST = _spec(
    "xgboost",
    "XGBoost scikit-learn-API estimators: full retraining",
    lambda s: XgboostType(frozenset({"xgboost"}), _budget(s)),
    distributions=("xgboost",),
    frameworks=frozenset({"xgboost"}),
)
TORCH = _spec(
    "torch",
    "tabular torch.nn.Module models: warm-start fine-tuning, full retraining",
    lambda s: TorchTabularType(TORCH_FRAMEWORKS, _budget(s)),
    keys=_TORCH_KEYS,
    distributions=("torch",),
    frameworks=TORCH_FRAMEWORKS,
)
LIGHTGBM = _spec(
    "lightgbm",
    "LightGBM scikit-learn-API models: continued boosting, full retraining",
    lambda s: BoostingType("lightgbm", init_from_booster=True),
    distributions=("lightgbm",),
    frameworks=frozenset({"lightgbm"}),
)
CATBOOST = _spec(
    "catboost",
    "CatBoost scikit-learn-API models: continued boosting, full retraining",
    lambda s: BoostingType("catboost", init_from_booster=False),
    distributions=("catboost",),
    frameworks=frozenset({"catboost"}),
)
