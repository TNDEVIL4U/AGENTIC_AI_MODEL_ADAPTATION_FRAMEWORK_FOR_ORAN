"""Conformance suite for ``ModelTypePort`` plugins (how one kind of model is inspected, adapted
and asked for predictions).

Each check takes a plugin and a ``Context`` and raises ConformanceFailure on a deviation. The
context holds a fitted model the plugin handles, its framework and a small training set in time
order (oldest row first)::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_type(check, tmp_path):
        CHECKS[check](MyType(...), Context(model=..., framework="myfw", X=X, y=y,
                                           target_column="y", workdir=str(tmp_path)))

docs/adapters/model_type.md explains each rule.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from unittest import mock

import numpy as np
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import AdaptationError, UnsupportedAdaptationError
from oran_adapt.ports import ModelTypePort, TrainingSet

_ESTIMATOR_TYPES = frozenset({"classifier", "regressor", "clusterer", "outlier_detector",
                              "unknown"})


@dataclass
class Context:
    model: object
    """A fitted model the plugin handles."""
    framework: str
    """The framework name the model is registered under (one of the plugin's frameworks)."""
    X: pd.DataFrame
    """Feature rows, oldest first."""
    y: pd.Series
    """The target, row-aligned with ``X``."""
    target_column: str
    workdir: str
    """A writable directory; each adaptation writes under its own subdirectory."""
    foreign: object = None
    """Something the plugin must not accept (None: a plain ``object()``)."""

    def data(self, label: str) -> TrainingSet:
        return TrainingSet(X=self.X, y=self.y, target_column=self.target_column,
                           artifact_dir=os.path.join(self.workdir, label))


def _inspection(port: ModelTypePort, ctx: Context) -> ModelInspection:
    inspection = port.inspect(ctx.model, ctx.framework)
    expect(isinstance(inspection, ModelInspection),
           f"inspect returned {type(inspection).__name__}, not ModelInspection")
    return inspection


def _predictions(port: ModelTypePort, model: object, ctx: Context,
                 inspection: ModelInspection) -> np.ndarray:
    predictions = port.predict(model, ctx.X, inspection=inspection)
    expect(isinstance(predictions, np.ndarray), "predict must return a numpy array")
    expect(predictions.ndim == 1 and len(predictions) == len(ctx.X),
           f"predict must return one value per row: shape {predictions.shape} for {len(ctx.X)} "
           "rows")
    return predictions


def _refuse_shuffle(what: str) -> Callable[..., Any]:
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ConformanceFailure(f"a temporal model was shuffled ({what}) while adapting")

    return refused


@contextmanager
def _no_shuffling() -> Iterator[None]:
    """Any random reordering of rows raises while this is active."""
    targets = [("numpy.random.permutation", "np.random.permutation"),
               ("numpy.random.shuffle", "np.random.shuffle")]
    if "torch" in sys.modules:
        targets.append(("torch.randperm", "torch.randperm"))
    if "sklearn" in sys.modules:
        targets += [("sklearn.model_selection.train_test_split", "train_test_split"),
                    ("sklearn.utils.shuffle", "sklearn.utils.shuffle")]
    with ExitStack() as stack:
        for target, what in targets:
            stack.enter_context(mock.patch(target, _refuse_shuffle(what)))
        yield


def check_protocol(port: ModelTypePort, ctx: Context) -> None:
    expect(isinstance(port, ModelTypePort), "does not implement ModelTypePort")
    frameworks = port.frameworks
    expect(bool(frameworks), "a model type must serve at least one framework")
    expect(all(isinstance(f, str) and f == f.lower() and f for f in frameworks),
           f"framework names must be non-empty lowercase strings: {sorted(frameworks)}")
    expect(ctx.framework.lower() in frameworks,
           f"the context's framework {ctx.framework!r} is not one the plugin serves")
    for strategy, engine in port.engines.items():
        expect(isinstance(strategy, Strategy), f"engine key {strategy!r} is not a Strategy")
        expect(isinstance(engine, str) and engine != "", f"engine name {engine!r} is not a name")


def check_accepts(port: ModelTypePort, ctx: Context) -> None:
    expect(port.accepts(ctx.model, ctx.framework) is True,
           f"the plugin does not accept its own {type(ctx.model).__name__} model")
    foreign = object() if ctx.foreign is None else ctx.foreign
    expect(port.accepts(foreign, ctx.framework) is False,
           f"the plugin accepts a {type(foreign).__name__}, which it cannot handle")


def check_inspect(port: ModelTypePort, ctx: Context) -> None:
    inspection = _inspection(port, ctx)
    expect(inspection.estimator_type in _ESTIMATOR_TYPES,
           f"unknown estimator type {inspection.estimator_type!r}")
    expect(inspection.model_class != "", "inspection must name the model class")
    if inspection.sequence_window is not None:
        expect(inspection.sequence_window >= 1,
               f"a sequence window must be at least 1, got {inspection.sequence_window}")
        expect(inspection.temporal, "a model that reads windows of rows must be temporal")


def check_predict(port: ModelTypePort, ctx: Context) -> None:
    inspection = _inspection(port, ctx)
    predictions = _predictions(port, ctx.model, ctx, inspection)
    again = _predictions(port, ctx.model, ctx, inspection)
    expect(np.array_equal(predictions, again), "predict is not repeatable on the same rows")


def check_adapt(port: ModelTypePort, ctx: Context) -> None:
    inspection = _inspection(port, ctx)
    before = _predictions(port, ctx.model, ctx, inspection)
    if not port.engines:
        try:
            port.adapt(ctx.model, Strategy.FULL_RETRAINING, inspection=inspection,
                       data=ctx.data("refused"))
        except UnsupportedAdaptationError:
            return
        raise ConformanceFailure(
            "a plugin with no engine must refuse to adapt with UnsupportedAdaptationError"
        )
    for strategy, engine in port.engines.items():
        candidate = port.adapt(ctx.model, strategy, inspection=inspection,
                               data=ctx.data(strategy.value.lower()))
        expect(isinstance(candidate, CandidateModel),
               f"adapt returned {type(candidate).__name__}, not CandidateModel")
        expect(str(candidate.engine) == engine,
               f"{strategy.value} produced engine {candidate.engine!r}, declared {engine!r}")
        expect(os.path.exists(candidate.artifact_path),
               f"the candidate artifact {candidate.artifact_path} was not written")
        expect(candidate.n_train_rows == len(ctx.X),
               f"n_train_rows is {candidate.n_train_rows}, trained on {len(ctx.X)}")
        expect(all(isinstance(v, float) and math.isfinite(v) for v in candidate.metrics.values()),
               f"training metrics must be finite floats: {candidate.metrics}")
    after = _predictions(port, ctx.model, ctx, inspection)
    expect(np.array_equal(before, after),
           "adapting changed the live model: a candidate must be a copy")


def check_time_order(port: ModelTypePort, ctx: Context) -> None:
    """A temporal model is never trained on a randomly reordered set of rows."""
    inspection = _inspection(port, ctx)
    if not inspection.temporal:
        return
    for strategy in port.engines:
        try:
            with _no_shuffling():
                port.adapt(ctx.model, strategy, inspection=inspection,
                           data=ctx.data(f"ordered-{strategy.value.lower()}"))
        except ConformanceFailure:
            raise
        except AdaptationError as exc:
            if isinstance(exc.__cause__, ConformanceFailure):
                raise exc.__cause__ from exc
            raise


CHECKS: dict[str, Callable[[ModelTypePort, Context], None]] = {
    "protocol": check_protocol,
    "accepts": check_accepts,
    "inspect": check_inspect,
    "predict": check_predict,
    "adapt": check_adapt,
    "time_order": check_time_order,
}


def run(port: ModelTypePort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
