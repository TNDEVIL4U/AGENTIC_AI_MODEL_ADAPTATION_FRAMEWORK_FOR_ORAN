"""Test doubles for model libraries that are not installed here (LightGBM, CatBoost, Keras), and
the small real models the Phase 8 tests use.

The model type plugins recognise LightGBM, CatBoost and Keras models by the module their class
comes from and never import the libraries (Keras only to clone a model). Each double is a class
whose ``__module__`` is the library's, registered in a stand-in module under that name while a
test runs (``install``), so the plugin, pickling and ``joblib`` treat it as the real thing. The
doubles honour the parts of each library's API the plugins use - ``fit(..., init_model=...)``
for continued boosting, ``booster_``, Keras' ``input_shape``/``output_shape``/``compile``/
``fit(shuffle=..., validation_data=...)``/``get_weights``/``set_weights`` and
``keras.models.clone_model`` - and nothing else.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.linear_model import Ridge
from torch import nn


class _Boosted(BaseEstimator, RegressorMixin):
    """Gradient boosting in miniature: each fit adds a ridge stage fitted to what the stages
    before it (``init_model``) leave unexplained."""

    def __init__(self, alpha: float = 1.0) -> None:
        self.alpha = alpha

    def fit(self, X: Any, y: Any, init_model: Any = None) -> _Boosted:
        base = np.zeros(len(X)) if init_model is None else np.asarray(init_model.predict(X))
        self.init_model_ = init_model
        self.stage_ = Ridge(alpha=self.alpha).fit(X, np.asarray(y, dtype=float) - base)
        return self

    def predict(self, X: Any) -> np.ndarray:
        base = 0.0 if self.init_model_ is None else np.asarray(self.init_model_.predict(X))
        return base + self.stage_.predict(X)

    @property
    def stages(self) -> int:
        return 1 + (0 if self.init_model_ is None else getattr(self.init_model_, "stages", 1))


class LGBMRegressor(_Boosted):
    """LightGBM continues boosting from the fitted ``booster_``."""

    @property
    def booster_(self) -> LGBMRegressor:
        return self


class CatBoostRegressor(_Boosted):
    """CatBoost continues boosting from the fitted model itself."""


class Adam:
    def __init__(self, learning_rate: float = 0.01) -> None:
        self.learning_rate = learning_rate

    def get_config(self) -> dict[str, float]:
        return {"learning_rate": self.learning_rate}

    @classmethod
    def from_config(cls, config: dict[str, float]) -> Adam:
        return cls(**config)


class Sequential:
    """A Keras model in miniature: a linear map over the flattened input window, "trained" by
    least squares. It records every ``fit`` call's keyword arguments."""

    def __init__(self, window: int, features: int, estimator_type: str = "regressor") -> None:
        self.window, self.features = window, features
        self.input_shape = (None, window, features) if window else (None, features)
        self.output_shape = (None, 1)
        self.w = np.zeros(max(window, 1) * features)
        self.b = 0.0
        self.optimizer: Adam | None = None
        self.loss: str | None = None
        self.fits: list[dict[str, Any]] = []

    def compile(self, optimizer: Adam, loss: str) -> None:
        self.optimizer, self.loss = optimizer, loss

    def count_params(self) -> int:
        return self.w.size + 1

    def get_weights(self) -> list[np.ndarray]:
        return [self.w.copy(), np.array(self.b)]

    def set_weights(self, weights: list[np.ndarray]) -> None:
        self.w, self.b = weights[0].copy(), float(weights[1])

    def _flat(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(x, dtype=float).reshape(len(x), -1)

    def fit(self, x: np.ndarray, y: np.ndarray, **kwargs: Any) -> None:
        self.fits.append(kwargs)
        design = np.hstack([self._flat(x), np.ones((len(x), 1))])
        solution, *_ = np.linalg.lstsq(design, np.asarray(y, dtype=float), rcond=None)
        self.w, self.b = solution[:-1], float(solution[-1])

    def predict(self, x: np.ndarray, verbose: int = 0) -> np.ndarray:
        return (self._flat(x) @ self.w + self.b).reshape(-1, 1)


def _clone_model(model: Sequential) -> Sequential:
    return Sequential(model.window, model.features)


_DOUBLES: dict[str, dict[str, Any]] = {
    "lightgbm": {"LGBMRegressor": LGBMRegressor},
    "catboost": {"CatBoostRegressor": CatBoostRegressor},
    "keras": {
        "Sequential": Sequential,
        "Adam": Adam,
        "models": types.SimpleNamespace(clone_model=_clone_model),
    },
}
for _library, _members in _DOUBLES.items():
    for _member in _members.values():
        if isinstance(_member, type):
            _member.__module__ = _library


def install(monkeypatch: Any) -> None:
    """Register the stand-in ``lightgbm``, ``catboost`` and ``keras`` modules for one test."""
    for library, members in _DOUBLES.items():
        module = types.ModuleType(library)
        for name, member in members.items():
            setattr(module, name, member)
        monkeypatch.setitem(sys.modules, library, module)


# ---- small real models --------------------------------------------------------------------------


class TinyLSTM(nn.Module):
    """An LSTM regressor over windows of ``sequence_window`` rows."""

    sequence_window = 4

    def __init__(self, n_features: int = 2, hidden: int = 8) -> None:
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden, batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])


class TinyTCN(nn.Module):
    """A causal 1-D convolution over the window (no declared window: SEQUENCE_WINDOW applies)."""

    def __init__(self, n_features: int = 2, window: int = 4) -> None:
        super().__init__()
        self.conv = nn.Conv1d(n_features, 4, kernel_size=window)
        self.head = nn.Linear(4, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(torch.relu(self.conv(x.transpose(1, 2))[:, :, -1]))


def series(n: int, *, offset: float = 0.0, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """A time series, oldest row first: y depends on the current and earlier rows of ``a`` and
    ``b``, so a model has to read a window to predict it."""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    a = np.sin(t / 6) + rng.normal(0, 0.05, n)
    b = np.cos(t / 9) + rng.normal(0, 0.05, n)
    lag = lambda v, k: np.concatenate([np.repeat(v[:1], k), v[:-k]])
    y = 0.8 * a + 0.6 * lag(a, 2) - 0.4 * lag(b, 1) + offset
    return pd.DataFrame({"a": a, "b": b}), pd.Series(y, name="kpi")
