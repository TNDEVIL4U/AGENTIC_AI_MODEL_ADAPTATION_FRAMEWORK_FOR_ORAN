"""Template: a model type plugin (docs/adapters/model_type.md).

Copy this file into your own package, rename ``FRAMEWORK`` and the classes, and register the spec
under the ``oran_adapt.model_type`` entry-point group::

    [project.entry-points."oran_adapt.model_type"]
    my-model = "my_package.adapter:SPEC"

The example serves a made-up framework, ``template-linear``: a least-squares linear model. It
is complete - it passes the conformance suite (oran_adapt.conformance.model_types) as it is -
so each method shows what the port expects. Replace the model with yours and keep the rules:

* ``accepts`` recognises the loaded object without importing heavy libraries when it can;
* ``inspect`` describes the model and sets ``temporal``/``sequence_window`` for any model that
  reads rows in time order;
* ``adapt`` never trains the live model in place, never shuffles a temporal model's rows, writes
  the candidate with joblib under ``data.artifact_dir`` and names the engine it declared;
* ``predict`` returns one value per row of ``X``.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any

import joblib
import numpy as np
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability, TrainingSet

FRAMEWORK = "template-linear"


class LinearModel:
    """The example model: weights over named features plus an intercept."""

    def __init__(self, features: list[str]) -> None:
        self.features = list(features)
        self.coef = np.zeros(len(features))
        self.intercept = 0.0

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LinearModel:
        design = np.hstack([X[self.features].to_numpy(dtype=float), np.ones((len(X), 1))])
        solution, *_ = np.linalg.lstsq(design, y.to_numpy(dtype=float), rcond=None)
        self.coef, self.intercept = solution[:-1], float(solution[-1])
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(X[self.features].to_numpy(dtype=float) @ self.coef + self.intercept)


class LinearType:
    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset({FRAMEWORK})

    @property
    def engines(self) -> Mapping[Strategy, str]:
        # Declare only the strategies you can run; an empty mapping means inference only.
        return {Strategy.FULL_RETRAINING: "TEMPLATE_LINEAR_REFIT"}

    def accepts(self, model: object, framework: str) -> bool:
        return isinstance(model, LinearModel)

    def inspect(self, model: object, framework: str) -> ModelInspection:
        linear: Any = model
        return ModelInspection(
            framework=FRAMEWORK,
            model_class=type(model).__name__,
            estimator_type="regressor",
            n_features_in=len(linear.features),
            feature_names_in=list(linear.features),
            n_parameters=len(linear.features) + 1,
        )

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        if strategy not in self.engines:
            raise UnsupportedAdaptationError(f"no {strategy.value} engine for {FRAMEWORK}")
        fresh: Any = copy.deepcopy(model)  # never train the live model
        fresh.fit(data.X, data.y)
        residual = fresh.predict(data.X) - data.y.to_numpy(dtype=float)
        os.makedirs(data.artifact_dir, exist_ok=True)
        path = os.path.join(data.artifact_dir, "model.joblib")
        joblib.dump(fresh, path)
        return CandidateModel(
            engine=self.engines[strategy],
            framework=FRAMEWORK,
            model_class=type(fresh).__name__,
            artifact_path=path,
            metrics={"rmse": float(np.sqrt(np.mean(residual**2)))},
            n_train_rows=len(data.X),
            feature_names=list(data.X.columns),
            target_column=data.target_column,
        )

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        linear: Any = model
        return np.asarray(linear.predict(X), dtype=float)


def example_model(X: pd.DataFrame, y: pd.Series) -> LinearModel:
    """A fitted example model, for the conformance run in the README."""
    return LinearModel(list(X.columns)).fit(X, y)


SPEC = AdapterSpec(
    capability=Capability(
        port="model_type",
        adapter="template-linear",
        description="template: least-squares linear models, full retraining",
        features=frozenset({"tabular", f"framework:{FRAMEWORK}"}),
    ),
    factory=lambda settings: LinearType(),
)
