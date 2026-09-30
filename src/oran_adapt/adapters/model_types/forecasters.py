"""Model type plugin ``statsmodels``: classical forecasters - fitted state-space results
(statsmodels ``MLEResults``: ARIMA, SARIMAX, UnobservedComponents, ETS, ...). Results without
``apply`` (e.g. Holt-Winters ``ExponentialSmoothing``) are not recognised and inspect as an
unsupported model type.

The target column is the series; the model's exogenous regressors, when it has any, are the
feature columns of the same names (fit the model on a pandas DataFrame so it keeps them). Rows
are the series in time order.

* Fine-tuning keeps the fitted parameters and runs the model's filter over the new series
  (``results.apply(..., refit=False)``): the state catches up with the data, nothing is
  re-estimated.
* Full retraining re-estimates the same specification (orders, trend, seasonality) on the new
  series (``results.apply(..., refit=True)``).
* A prediction for the rows of ``X`` is the multi-step forecast from the end of the series the
  model last saw, one step per row, with the rows' exogenous values.

statsmodels is imported only here.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import joblib
import numpy as np

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import TrainingSet


def _exog_names(results: Any) -> list[str] | None:
    names = getattr(results.model, "exog_names", None)
    return list(names) if names else None


class StatsmodelsType:
    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset({"statsmodels"})

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return {
            Strategy.FINE_TUNING: "STATSMODELS_FILTER_UPDATE",
            Strategy.FULL_RETRAINING: "STATSMODELS_REFIT",
        }

    def accepts(self, model: object, framework: str) -> bool:
        return (
            type(model).__module__.split(".", 1)[0] == "statsmodels"
            and all(hasattr(model, a) for a in ("apply", "forecast", "model", "params"))
        )

    def inspect(self, model: object, framework: str) -> ModelInspection:
        results: Any = model
        exog = _exog_names(results)
        return ModelInspection(
            framework="statsmodels",
            model_class=type(results.model).__name__,
            estimator_type="regressor",
            n_features_in=len(exog) if exog else 0,
            feature_names_in=exog,
            supports_warm_start=True,
            n_parameters=len(np.atleast_1d(results.params)),
            temporal=True,
        )

    def _exog(self, results: Any, X: pd.DataFrame) -> np.ndarray | None:
        names = _exog_names(results)
        return None if names is None else X[names].to_numpy(dtype=float)

    def _named_exog(self, results: Any, X: pd.DataFrame) -> pd.DataFrame | None:
        # A named frame, so the refitted model keeps the regressor names (a bare array would
        # rename them x1, x2, ...); positions restart at 0 like the endog array.
        names = _exog_names(results)
        return None if names is None else X[names].astype(float).reset_index(drop=True)

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        if strategy not in self.engines:
            raise UnsupportedAdaptationError(f"no {strategy.value} engine for forecasters")
        results: Any = model
        endog = data.y.to_numpy(dtype=float)
        try:
            with warnings.catch_warnings():
                # Convergence and index notices from the estimator; the fit is judged on the
                # hold-out, not on its warnings.
                warnings.simplefilter("ignore")
                fitted = results.apply(
                    endog,
                    exog=self._named_exog(results, data.X),
                    refit=strategy == Strategy.FULL_RETRAINING,
                )
        except Exception as exc:
            raise ArtifactError(
                f"refitting the {inspection.model_class} forecaster failed", cause=str(exc)
            ) from exc
        in_sample = np.asarray(fitted.fittedvalues, dtype=float)
        rmse = float(np.sqrt(np.mean((in_sample - endog) ** 2)))
        os.makedirs(data.artifact_dir, exist_ok=True)
        path = os.path.join(data.artifact_dir, "model.joblib")
        joblib.dump(fitted, path)
        return CandidateModel(
            engine=self.engines[strategy],
            framework="statsmodels",
            model_class=inspection.model_class,
            artifact_path=path,
            metrics={"rmse": rmse},
            n_train_rows=len(data.X),
            feature_names=list(data.X.columns),
            target_column=data.target_column,
        )

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        if len(X) == 0:
            return np.zeros(0)
        results: Any = model
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            forecast = results.forecast(steps=len(X), exog=self._exog(results, X))
        return np.asarray(forecast, dtype=float)


def _build(settings: Settings) -> StatsmodelsType:
    return StatsmodelsType()


SPEC = AdapterSpec(
    capability=Capability(
        port="model_type",
        adapter="statsmodels",
        description="classical state-space forecasters (ARIMA, SARIMAX, ...): filter update, refit",
        features=frozenset({"forecaster", "temporal", "framework:statsmodels"}),
        distributions=("statsmodels",),
    ),
    factory=_build,
)
