"""Hardening Phase 8: model types behind a port.

Every kind of model - scikit-learn, XGBoost, LightGBM, CatBoost, tabular torch, ONNX, torch and
Keras sequence models, classical forecasters - is handled by a model type plugin
(oran_adapt.ports.ModelTypePort) found through the ``oran_adapt.model_type`` entry points. The
tests below hold the gate: a small sequence model goes inspect -> retrain -> evaluate -> gate;
an unsupported model gives a typed error with no stack trace; temporal models are never trained
on a random split; and a plugin installed from outside the package works with no core change.

LightGBM, CatBoost and Keras are not installed here: their plugins run against the doubles in
model_type_doubles (the real libraries are unverified locally). ONNX graphs run on the onnx
package's reference evaluator; onnxruntime is not installed.
"""

from __future__ import annotations

import inspect as pyinspect
import json
import os
import re
import sys
from pathlib import Path

import joblib
import model_type_doubles as doubles
import numpy as np
import pandas as pd
import pytest
import test_phase9_orchestrator as phase9
import torch
from onnx import TensorProto, helper, numpy_helper
from sklearn.linear_model import SGDRegressor
from xgboost import XGBRegressor

from oran_adapt import plugins
from oran_adapt.adaptation import model_types as mt
from oran_adapt.adaptation.capability import assess_capability
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.model_types import (
    ModelTypes,
    build_model_types,
    default_model_types,
    supported_frameworks,
)
from oran_adapt.adaptation.schemas import UnsupportedModelType
from oran_adapt.adaptation.sequence import sliding_windows, time_ordered_split, training_windows
from oran_adapt.adapters.model_types import forecasters, onnx, sequence, tabular
from oran_adapt.analysis.summary import framework_capabilities
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance import model_types as conformance
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import (
    AdaptationError,
    ArtifactError,
    ConfigurationError,
    UnsupportedAdaptationError,
    UnsupportedModelTypeError,
)
from oran_adapt.ports import PORTS, ModelTypePort, TrainingSet
from oran_adapt.validation.engine import validate_candidate
from oran_adapt.validation.evaluate import evaluate_model

ROOT = Path(__file__).resolve().parents[2]
# The Phase 9 orchestrator fixtures: a migrated database and an MLflow registry.
session_factory = phase9.session_factory
registry = phase9.registry
BUILTIN = {"catboost", "keras", "lightgbm", "onnx", "sklearn", "statsmodels", "torch",
           "torch-sequence", "xgboost"}


def _settings(**overrides) -> Settings:
    base = {"sequence_fine_tune_epochs": 20, "sequence_full_retrain_epochs": 150,
            "onnx_runtime": "reference"}
    return Settings(_env_file=None, **{**base, **overrides})


def _types(**overrides) -> ModelTypes:
    return build_model_types(_settings(**overrides))


def _tabular(n: int = 120, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n), "c": rng.normal(size=n)})
    return X, pd.Series(2 * X["a"] - X["b"] + 0.1 * rng.normal(size=n), name="kpi")


# ---- the port and its registry --------------------------------------------------------------


def test_model_type_is_a_port_with_every_builtin_plugin() -> None:
    assert PORTS["model_type"] is ModelTypePort
    assert set(plugins.adapters("model_type")) == BUILTIN
    types = _types()
    assert set(types.installed) == BUILTIN
    assert all(isinstance(t, ModelTypePort) for t in types.types.values())
    assert types.frameworks(Strategy.FINE_TUNING) >= {"sklearn", "torch", "lightgbm", "keras",
                                                      "statsmodels"}
    assert "xgboost" not in types.frameworks(Strategy.FINE_TUNING)
    assert "onnx" not in types.frameworks()  # inference only


def test_model_types_setting_selects_and_orders_plugins() -> None:
    types = _types(model_types=["torch-sequence", "sklearn"])
    assert list(types.types) == ["torch-sequence", "sklearn"]
    with pytest.raises(ConfigurationError) as info:
        _types(model_types=["sklearn", "nope"])
    assert info.value.context["key"] == "MODEL_TYPES"


def test_supported_frameworks_default_to_the_installed_plugins() -> None:
    assert supported_frameworks(_settings()) == _types().frameworks()
    assert supported_frameworks(_settings(decision_supported_frameworks=["SKLEARN"])) == {"sklearn"}
    caps = framework_capabilities("statsmodels", _settings())
    assert caps["fine_tuning"] and caps["full_retraining"]
    assert framework_capabilities("onnx", _settings())["full_retraining"] is False


# ---- conformance: every plugin, on a model it handles -----------------------------------------


def _onnx_linear(width: int = 3) -> object:
    weights = numpy_helper.from_array(np.array([[2.0], [-1.0], [0.0]], dtype=np.float32), "W")
    graph = helper.make_graph(
        [helper.make_node("MatMul", ["X", "W"], ["Y"])],
        "linear",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, width])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [None, 1])],
        [weights],
    )
    return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])


def _sarimax() -> tuple[object, pd.DataFrame, pd.Series]:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    X, y = doubles.series(120)
    results = SARIMAX(y, exog=X[["a"]], order=(1, 0, 0)).fit(disp=False)
    return results, X, y


def _context(name: str, tmp_path: Path) -> tuple[ModelTypePort, conformance.Context]:
    settings = _settings()
    port = plugins.adapters("model_type")[name].factory(settings)
    X, y = _tabular()
    if name == "sklearn":
        model, fw = SGDRegressor(random_state=0).fit(X, y), "sklearn"
    elif name == "xgboost":
        model, fw = XGBRegressor(n_estimators=5, max_depth=2).fit(X, y), "xgboost"
    elif name == "torch":
        torch.manual_seed(0)
        model, fw = torch.nn.Sequential(torch.nn.Linear(3, 4), torch.nn.ReLU(),
                                        torch.nn.Linear(4, 1)), "torch"
    elif name in ("lightgbm", "catboost"):
        cls = doubles.LGBMRegressor if name == "lightgbm" else doubles.CatBoostRegressor
        model, fw = cls().fit(X, y), name
    elif name == "onnx":
        model, fw = _onnx_linear(), "onnx"
    elif name == "torch-sequence":
        torch.manual_seed(0)
        X, y = doubles.series(80)
        model, fw = doubles.TinyLSTM(), "pytorch"
    elif name == "keras":
        X, y = doubles.series(80)
        model, fw = doubles.Sequential(window=4, features=2), "keras"
        model.compile(doubles.Adam(), "mse")
        model.fit(sliding_windows(X.to_numpy(), 4), y.to_numpy())
    else:
        model, X, y = _sarimax()
        fw = "statsmodels"
    return port, conformance.Context(model=model, framework=fw, X=X, y=y, target_column="kpi",
                                     workdir=str(tmp_path))


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_builtin_model_type_conformance(name, tmp_path, monkeypatch) -> None:
    doubles.install(monkeypatch)
    port, ctx = _context(name, tmp_path)
    assert conformance.run(port, ctx) == list(conformance.CHECKS)


def test_conformance_catches_a_plugin_that_shuffles_a_temporal_model(tmp_path) -> None:
    class Shuffling(sequence.TorchSequenceType):
        def adapt(self, model, strategy, *, inspection, data):
            order = np.random.permutation(len(data.X))
            shuffled = TrainingSet(data.X.iloc[order], data.y.iloc[order], data.target_column,
                                   data.artifact_dir)
            return super().adapt(model, strategy, inspection=inspection, data=shuffled)

    _, ctx = _context("torch-sequence", tmp_path)
    with pytest.raises(ConformanceFailure, match="shuffled"):
        conformance.check_time_order(Shuffling(_settings()), ctx)


def test_conformance_catches_a_plugin_that_trains_the_live_model(tmp_path, monkeypatch) -> None:
    doubles.install(monkeypatch)
    class InPlace(tabular.BoostingType):
        def adapt(self, model, strategy, *, inspection, data):
            model.fit(data.X, data.y + 5.0)
            return super().adapt(model, strategy, inspection=inspection, data=data)

    X, y = _tabular()
    ctx = conformance.Context(model=doubles.LGBMRegressor().fit(X, y), framework="lightgbm",
                              X=X, y=y, target_column="kpi", workdir=str(tmp_path))
    with pytest.raises(ConformanceFailure, match="changed the live model"):
        conformance.check_adapt(InPlace("lightgbm", init_from_booster=True), ctx)


# ---- per-plugin behaviour ---------------------------------------------------------------------


def test_continued_boosting_starts_from_the_current_model(monkeypatch, tmp_path) -> None:
    doubles.install(monkeypatch)
    X, y = _tabular()
    types = _types()
    for cls, fw in ((doubles.LGBMRegressor, "lightgbm"), (doubles.CatBoostRegressor, "catboost")):
        current = cls().fit(X, y)
        inspection = types.require(current, fw)
        assert inspection.model_type == fw and inspection.supports_warm_start
        capability = assess_capability(inspection, list(X.columns))
        data = TrainingSet(X, y, "kpi", str(tmp_path / fw))
        tuned = types.adapt(current, Strategy.FINE_TUNING, inspection=inspection,
                            capability=capability, data=data)
        assert tuned.engine == f"{fw.upper()}_CONTINUED_BOOSTING"
        assert joblib.load(tuned.artifact_path).stages == 2
        fresh = types.adapt(current, Strategy.FULL_RETRAINING, inspection=inspection,
                            capability=capability, data=data)
        assert joblib.load(fresh.artifact_path).stages == 1


def test_keras_sequence_training_is_never_shuffled_and_uses_a_time_ordered_tail(
    monkeypatch, tmp_path
) -> None:
    doubles.install(monkeypatch)
    X, y = doubles.series(60)
    net = doubles.Sequential(window=4, features=2)
    net.compile(doubles.Adam(), "mse")
    types = _types()
    inspection = types.require(net, "keras")
    assert inspection.temporal and inspection.sequence_window == 4
    candidate = types.adapt(net, Strategy.FINE_TUNING, inspection=inspection,
                            capability=assess_capability(inspection, ["a", "b"]),
                            data=TrainingSet(X, y, "kpi", str(tmp_path)))
    trained = joblib.load(candidate.artifact_path)
    (call,) = trained.fits
    assert call["shuffle"] is False
    _, held_y = call["validation_data"]
    _, targets = training_windows(X.to_numpy(), y.to_numpy(), 4)
    assert np.array_equal(held_y, targets[-len(held_y):])  # the newest windows validate
    assert net.fits == []  # the live model was not trained


def test_keras_uncompiled_and_tabular(monkeypatch, tmp_path) -> None:
    doubles.install(monkeypatch)
    X, y = _tabular()
    net = doubles.Sequential(window=0, features=3)
    inspection = _types().require(net, "tensorflow")
    assert not inspection.temporal and inspection.sequence_window is None
    with pytest.raises(UnsupportedAdaptationError, match="not compiled"):
        sequence.KerasType(_settings()).adapt(net, Strategy.FULL_RETRAINING,
                                              inspection=inspection,
                                              data=TrainingSet(X, y, "kpi", str(tmp_path)))


def test_torch_models_are_split_between_tabular_and_sequence_plugins() -> None:
    types = _types()
    assert types.require(doubles.TinyLSTM(), "torch").model_type == "torch-sequence"
    tcn = types.require(doubles.TinyTCN(), "pytorch")
    assert tcn.model_type == "torch-sequence" and tcn.sequence_window == 8  # SEQUENCE_WINDOW
    mlp = torch.nn.Sequential(torch.nn.Linear(3, 1))
    assert types.require(mlp, "torch").model_type == "torch"
    assert sequence.is_torch_sequence(torch.nn.TransformerEncoderLayer(4, 2))


def test_onnx_is_scored_but_never_retrained(tmp_path) -> None:
    types = _types()
    graph = _onnx_linear()
    inspection = types.require(graph, "onnx")
    assert inspection.estimator_type == "regressor" and inspection.n_features_in == 3
    assert inspection.n_parameters == 3
    X, y = _tabular()
    scores = evaluate_model(graph, X, 2 * X["a"] - X["b"], framework="onnx",
                            estimator_type="regressor", model_types=types)
    assert scores["rmse"] < 1e-5
    with pytest.raises(UnsupportedAdaptationError, match="has no FULL_RETRAINING engine"):
        types.adapt(graph, Strategy.FULL_RETRAINING, inspection=inspection,
                    capability=assess_capability(inspection, list(X.columns)),
                    data=TrainingSet(X, y, "kpi", str(tmp_path)))


def test_onnx_label_output_makes_a_classifier() -> None:
    graph = helper.make_graph(
        [helper.make_node("ArgMax", ["X"], ["label"], axis=1, keepdims=0)],
        "argmax",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, 2])],
        [helper.make_tensor_value_info("label", TensorProto.INT64, [None])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    types = _types()
    inspection = types.require(model, "onnx")
    assert inspection.estimator_type == "classifier"
    X = pd.DataFrame({"p": [0.9, 0.1], "q": [0.1, 0.9]})
    assert list(types.predict(model, X, inspection=inspection)) == [0, 1]


def test_statsmodels_filter_update_and_refit(tmp_path) -> None:
    results, _, _ = _sarimax()
    types = _types()
    inspection = types.require(results, "statsmodels")
    assert inspection.temporal and inspection.feature_names_in == ["a"]
    newer_X, newer_y = doubles.series(140, offset=0.3, seed=1)
    capability = assess_capability(inspection, list(newer_X.columns))
    X_new, y_new = newer_X.iloc[:100], newer_y.iloc[:100]
    updated = types.adapt(results, Strategy.FINE_TUNING, inspection=inspection,
                          capability=capability,
                          data=TrainingSet(X_new, y_new, "kpi", str(tmp_path / "update")))
    refit = types.adapt(results, Strategy.FULL_RETRAINING, inspection=inspection,
                        capability=capability,
                        data=TrainingSet(X_new, y_new, "kpi", str(tmp_path / "refit")))
    assert updated.engine == "STATSMODELS_FILTER_UPDATE" and refit.engine == "STATSMODELS_REFIT"
    kept = joblib.load(updated.artifact_path)
    assert np.allclose(kept.params, results.params)  # filtered, not re-estimated
    assert not np.allclose(joblib.load(refit.artifact_path).params, results.params)
    forecast = types.predict(kept, newer_X.iloc[100:], inspection=inspection)
    assert forecast.shape == (40,) and np.isfinite(forecast).all()


def test_holt_winters_is_an_unsupported_model_type() -> None:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    _, y = doubles.series(40)
    fitted = ExponentialSmoothing(y.to_numpy()).fit()
    result = _types().inspect(fitted, "statsmodels")
    assert isinstance(result, UnsupportedModelType)


# ---- the gate: a sequence model through inspect -> retrain -> evaluate -> gate ---------------


def test_sequence_model_inspect_retrain_evaluate_gate(tmp_path) -> None:
    torch.manual_seed(0)
    types = _types(sequence_fine_tune_epochs=100)
    old_X, old_y = doubles.series(300, seed=2)
    current = doubles.TinyLSTM()
    inspection = types.require(current, "torch")
    assert inspection.model_type == "torch-sequence" and inspection.temporal
    assert inspection.sequence_window == 4 and inspection.input_dim == 2
    capability = assess_capability(inspection, ["a", "b"])
    seeded = types.adapt(current, Strategy.FULL_RETRAINING, inspection=inspection,
                         capability=capability,
                         data=TrainingSet(old_X, old_y, "kpi", str(tmp_path / "old")))
    current = joblib.load(seeded.artifact_path)

    # The world moves: the same dynamics, a level shift. Train on the older rows, hold the
    # newest back - in time order - for the gate.
    X, y = doubles.series(400, offset=0.8, seed=3)
    train_X, train_y, hold_X, hold_y = X.iloc[:300], y.iloc[:300], X.iloc[300:], y.iloc[300:]
    inspection = types.require(current, "torch")
    candidate = types.adapt(current, Strategy.FINE_TUNING, inspection=inspection,
                            capability=assess_capability(inspection, ["a", "b"]),
                            data=TrainingSet(train_X, train_y, "kpi", str(tmp_path / "new")))
    assert candidate.engine == "TORCH_SEQUENCE_FINE_TUNE" and candidate.framework == "torch"

    before = evaluate_model(current, hold_X, hold_y, framework="torch",
                            estimator_type="regressor", model_types=types)
    after = evaluate_model(joblib.load(candidate.artifact_path), hold_X, hold_y,
                           framework="torch", estimator_type="regressor", model_types=types)
    assert after["rmse"] < before["rmse"]

    report = validate_candidate(candidate, current, model_id="seq", X=hold_X, y=hold_y,
                                current_framework="torch", estimator_type="regressor",
                                settings=_settings(sequence_fine_tune_epochs=100))
    assert report.passed is True and report.gate is not None
    assert report.gate.verdict == "ACCEPT"


# ---- an unsupported model: a typed result, never a crash -------------------------------------


def test_unsupported_model_gives_a_typed_result_with_no_stack_trace() -> None:
    types = _types()
    unknown = types.inspect(object(), "prophet")
    assert isinstance(unknown, UnsupportedModelType)
    assert unknown.kind == "unsupported_model_type" and "prophet" in unknown.reason
    assert set(unknown.installed) == BUILTIN
    wrong = types.inspect(object(), "sklearn")
    assert isinstance(wrong, UnsupportedModelType) and "sklearn" in wrong.reason

    with pytest.raises(UnsupportedModelTypeError) as info:
        types.require(object(), "prophet")
    error = info.value
    assert isinstance(error, UnsupportedAdaptationError) and isinstance(error, AdaptationError)
    payload = error.to_dict()
    assert payload["code"] == "UNSUPPORTED_MODEL_TYPE"
    assert payload["context"]["kind"] == "unsupported_model_type"
    text = json.dumps(payload)
    assert "Traceback" not in text and 'File "' not in text
    # The inspector keeps its contract and raises the same typed error.
    with pytest.raises(UnsupportedModelTypeError):
        inspect_model(object(), "prophet")


def test_a_plugin_that_crashes_while_inspecting_is_reported_not_raised() -> None:
    class Broken(tabular.SklearnType):
        def inspect(self, model, framework):
            raise RuntimeError("boom")

    class Picky(tabular.SklearnType):
        def accepts(self, model, framework):
            raise ValueError("cannot tell")

    budget = tabular.TorchBudget.from_settings(_settings())
    X, y = _tabular()
    model = SGDRegressor().fit(X, y)
    result = ModelTypes({"broken": Broken(frozenset({"sklearn"}), budget)}).inspect(
        model, "sklearn")
    assert isinstance(result, UnsupportedModelType) and "boom" in result.reason
    result = ModelTypes({"picky": Picky(frozenset({"sklearn"}), budget)}).inspect(
        model, "sklearn")
    assert isinstance(result, UnsupportedModelType) and "picky" in result.reason


@pytest.mark.heavy  # a full pipeline job; the gate's acceptance check 2 covers the typed error
def test_unsupported_model_fails_the_job_with_the_typed_error(
    session_factory, registry, migrated_settings, tmp_path, monkeypatch
) -> None:
    """A real job on a model no configured plugin handles. Version scoring records the typed
    UNSUPPORTED_MODEL_TYPE reason and the decision layer declines to adapt; if inspection is
    reached anyway, the job stops with UnsupportedModelTypeError, which the worker records as
    ``to_dict()`` - a code, a message and a context, no stack trace."""
    from oran_adapt.core.schemas import DriftEvent
    from oran_adapt.db.base import session_scope
    from oran_adapt.orchestrator import pipeline

    historical = phase9._frame(40, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = phase9._frame(40, prb_lo=5.0, prb_hi=6.0, prb_seed=3, rsrp_seed=11)
    phase9._seed_model(session_factory, registry, migrated_settings, model_id="odd-model",
                       mlflow_name="odd_model_mlflow", historical=historical, drifted=drifted,
                       warm_start=False)
    event = DriftEvent(model_id="odd-model", drift_detected=True)
    only_onnx = migrated_settings.model_copy(update={"model_types": ["onnx"]})
    with session_scope(session_factory) as session:
        result = pipeline.run_adaptation_job(session, event, only_onnx, registry=registry,
                                             llm_client=None, workdir=str(tmp_path / "decided"))
    assert result.outcome == "NO_ACTION" and result.candidate is None
    assert result.decision is not None
    assert result.decision.evidence["model_capability"] == {
        "fine_tuning": False, "full_retraining": False, "llm_adapter": False}
    assert "UNSUPPORTED_MODEL_TYPE" in (result.decision.evidence["reuse_reason"] or "")

    # The decision layer sees every plugin, the adaptation step only onnx: inspection fails.
    monkeypatch.setattr(pipeline, "build_model_types",
                        lambda settings: build_model_types(only_onnx))
    with session_scope(session_factory) as session, pytest.raises(
        UnsupportedModelTypeError
    ) as info:
        pipeline.run_adaptation_job(session, event, migrated_settings, registry=registry,
                                    llm_client=None, workdir=str(tmp_path / "inspected"))
    payload = info.value.to_dict()
    assert payload["code"] == "UNSUPPORTED_MODEL_TYPE"
    assert payload["context"]["installed"] == {"onnx": ["onnx"]}
    assert "Traceback" not in json.dumps(payload)


# ---- temporal models are never trained on a random split -------------------------------------


def test_time_ordered_split_and_windows() -> None:
    train, held = time_ordered_split(10, 0.2)
    assert list(train) == list(range(8)) and list(held) == [8, 9]
    assert time_ordered_split(1, 0.5) == (range(1), range(1, 1))
    assert len(time_ordered_split(5, 0.99)[0]) == 1  # never all rows
    X = np.arange(10, dtype=float).reshape(5, 2)
    windows = sliding_windows(X, 3)
    assert windows.shape == (5, 3, 2)
    assert windows[0].tolist() == [[0, 1], [0, 1], [0, 1]]  # edge padding, no look-ahead
    assert windows[4].tolist() == [[4, 5], [6, 7], [8, 9]]
    Xw, yw = training_windows(X, np.arange(5), 3)
    assert len(Xw) == 3 and yw.tolist() == [2, 3, 4]
    with pytest.raises(ArtifactError):
        training_windows(X, np.arange(5), 6)
    with pytest.raises(ArtifactError):
        sliding_windows(X, 0)


def test_no_random_split_on_temporal_tasks(monkeypatch, tmp_path) -> None:
    """Every temporal plugin adapts with every random reordering of rows disabled, and splits
    through time_ordered_split; neither they nor the pipeline's hold-out ever shuffle."""
    doubles.install(monkeypatch)
    calls: list[tuple[int, float]] = []
    real = sequence.time_ordered_split

    def spy(n: int, fraction: float):
        calls.append((n, fraction))
        return real(n, fraction)

    monkeypatch.setattr(sequence, "time_ordered_split", spy)
    for name in ("torch-sequence", "keras", "statsmodels"):
        port, ctx = _context(name, tmp_path / name)
        conformance.check_time_order(port, ctx)
    assert calls, "the sequence plugins must split through time_ordered_split"
    for module in (sequence, forecasters):
        text = pyinspect.getsource(module)
        for forbidden in ("permutation", "randperm", "train_test_split", "shuffle=True",
                          "random.shuffle", "sample(frac"):
            assert forbidden not in text, (module.__name__, forbidden)
    pipeline_text = (ROOT / "src/oran_adapt/orchestrator/pipeline.py").read_text("utf-8")
    assert "train_test_split" not in pipeline_text and "permutation" not in pipeline_text


# ---- a plugin from outside the package, with zero core changes ------------------------------

_FIXTURE_PLUGIN = '''
import os

import joblib
import numpy as np

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.core.enums import Strategy
from oran_adapt.ports import AdapterSpec, Capability


class MeanModel:
    def __init__(self, mean=0.0):
        self.mean = mean


class MeanType:
    frameworks = frozenset({"meanfw"})
    engines = {Strategy.FULL_RETRAINING: "MEAN_REFIT"}

    def accepts(self, model, framework):
        return isinstance(model, MeanModel)

    def inspect(self, model, framework):
        return ModelInspection(framework="meanfw", model_class="MeanModel",
                               estimator_type="regressor")

    def adapt(self, model, strategy, *, inspection, data):
        os.makedirs(data.artifact_dir, exist_ok=True)
        path = os.path.join(data.artifact_dir, "model.joblib")
        joblib.dump(MeanModel(float(data.y.mean())), path)
        return CandidateModel(engine="MEAN_REFIT", framework="meanfw", model_class="MeanModel",
                              artifact_path=path, metrics={}, n_train_rows=len(data.X),
                              feature_names=list(data.X.columns),
                              target_column=data.target_column)

    def predict(self, model, X, *, inspection):
        return np.full(len(X), model.mean)


SPEC = AdapterSpec(
    capability=Capability(port="model_type", adapter="fixture-mean",
                          description="predicts the training mean",
                          features=frozenset({"tabular", "framework:meanfw"})),
    factory=lambda settings: MeanType(),
)
'''


@pytest.fixture
def fixture_plugin(tmp_path, monkeypatch):
    """Install a model type plugin the way a third party would: a distribution on sys.path
    whose entry point names it."""
    site = tmp_path / "site"
    (site / "fixture_mean-0.1.dist-info").mkdir(parents=True)
    (site / "fixture_mean_plugin.py").write_text(_FIXTURE_PLUGIN, encoding="utf-8")
    (site / "fixture_mean-0.1.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-mean\nVersion: 0.1\n", encoding="utf-8")
    (site / "fixture_mean-0.1.dist-info" / "entry_points.txt").write_text(
        "[oran_adapt.model_type]\nfixture-mean = fixture_mean_plugin:SPEC\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    plugins.adapters.cache_clear()
    default_model_types.cache_clear()
    yield sys.modules.get("fixture_mean_plugin")
    plugins.adapters.cache_clear()
    default_model_types.cache_clear()
    sys.modules.pop("fixture_mean_plugin", None)


def test_a_fixture_plugin_needs_no_core_change(fixture_plugin, tmp_path) -> None:
    types = _types()
    assert "fixture-mean" in types.installed
    module = sys.modules["fixture_mean_plugin"]
    model = module.MeanModel(1.0)
    inspection = types.require(model, "meanfw")
    assert inspection.model_type == "fixture-mean"
    X, y = _tabular()
    candidate = types.adapt(model, Strategy.FULL_RETRAINING, inspection=inspection,
                            capability=assess_capability(inspection, list(X.columns)),
                            data=TrainingSet(X, y, "kpi", str(tmp_path)))
    fitted = joblib.load(candidate.artifact_path)
    scores = evaluate_model(fitted, X, y, framework="meanfw", estimator_type="regressor",
                            model_types=types)
    assert scores["rmse"] > 0
    # The decision layer and the drift summary see the new framework too.
    assert "meanfw" in supported_frameworks(_settings())
    assert framework_capabilities("meanfw", _settings())["full_retraining"] is True
    assert conformance.run(types.types["fixture-mean"], conformance.Context(
        model=model, framework="meanfw", X=X, y=y, target_column="kpi",
        workdir=str(tmp_path / "conformance"))) == list(conformance.CHECKS)


# ---- serialization of the new frameworks -----------------------------------------------------


def test_native_handler_round_trips_onnx_and_statsmodels(tmp_path) -> None:
    from oran_adapt.adapters.handlers.native import NativeHandler

    handler = NativeHandler(_settings().mlflow_skops_trusted_types)
    graph = _onnx_linear()
    loaded = handler.load(handler.save(graph, "onnx", str(tmp_path / "onnx")), "onnx")
    assert loaded == graph
    results, _, _ = _sarimax()
    back = handler.load(handler.save(results, "statsmodels", str(tmp_path / "sm")), "statsmodels")
    assert np.allclose(back.params, results.params)
    assert {"lightgbm", "catboost", "keras", "tensorflow"} <= handler.frameworks


def test_mlflow_flavor_without_its_library_is_a_typed_error() -> None:
    from oran_adapt.adapters.registry.mlflow import flavors

    assert {"lightgbm", "catboost", "onnx", "statsmodels", "keras"} <= set(flavors.FLAVORS)
    with pytest.raises(UnsupportedAdaptationError):
        flavors._flavor("unknown-framework")
    try:
        flavors._flavor("keras")
    except UnsupportedAdaptationError as exc:  # keras is not installed here
        assert "flavor cannot be used" in exc.message
    assert flavors._flavor("onnx").__name__ == "mlflow.onnx"


# ---- configuration ---------------------------------------------------------------------------


def test_model_type_settings_validate() -> None:
    s = Settings(_env_file=None)
    assert s.model_types == [] and s.decision_supported_frameworks == []
    assert s.sequence_window == 8 and s.onnx_runtime == "onnxruntime"
    for bad in ({"sequence_window": 0}, {"sequence_validation_fraction": 1.0},
                {"onnx_runtime": "tensorrt"}, {"sequence_learning_rate": 0}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **bad)
    spec = plugins.adapters("model_type")["onnx"].capability
    assert spec.config_keys == ("onnx_runtime",)
    assert onnx.OnnxType("reference").runtime == "reference"


def test_the_template_plugin_passes_conformance(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "templates" / "model-type-adapter"))
    import adapter as template  # type: ignore[import-not-found]

    try:
        port = template.SPEC.factory(_settings())
        X, y = _tabular()
        model = template.example_model(X, y)
        ctx = conformance.Context(model=model, framework=template.FRAMEWORK, X=X, y=y,
                                  target_column="kpi", workdir=str(tmp_path))
        assert conformance.run(port, ctx) == list(conformance.CHECKS)
    finally:
        sys.modules.pop("adapter", None)


def test_every_plugin_module_imports_no_vendor_sdk_at_top_level() -> None:
    top_level = re.compile(r"^(import|from) (lightgbm|catboost|onnxruntime|tensorflow|keras)\b",
                           re.MULTILINE)
    for module in (tabular, sequence, onnx, forecasters, mt):
        assert not top_level.search(pyinspect.getsource(module)), module.__name__
    assert os.path.isfile(ROOT / "docs" / "adapters" / "model_type.md")
