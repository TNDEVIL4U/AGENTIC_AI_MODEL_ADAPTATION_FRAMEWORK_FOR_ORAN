"""Model type plugins for sequence models: a prediction reads a window of consecutive rows.

* ``torch-sequence`` - a ``torch.nn.Module`` taking ``(batch, window, features)``: anything
  with a recurrent (RNN/LSTM/GRU), 1-D convolutional (TCN) or attention / transformer layer,
  or any module declaring an integer ``sequence_window``. The window is the module's
  ``sequence_window`` when it declares one, SEQUENCE_WINDOW otherwise; the candidate is stamped
  with the window it was trained on.
* ``keras`` - a compiled Keras model. An input of rank 3 ``(batch, window, features)`` makes it
  a sequence model with the window its input shape fixes (SEQUENCE_WINDOW when that is open); a
  rank-2 input is tabular. The live model is never trained in place: it is cloned, the clone
  keeps its weights for fine-tuning and starts from fresh ones for a full retrain, and is
  compiled with the same optimizer settings and loss. Keras is imported only here.

Windowing lives here, not in the core: window ``i`` holds rows ``i - window + 1`` to ``i``
(oran_adapt.adaptation.sequence), the target is row ``i``'s. Training uses only windows with a
full real history; the newest SEQUENCE_VALIDATION_FRACTION of them are held back, in time
order, and the weights of the epoch that did best on them are kept. Nothing is shuffled.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

import joblib
import numpy as np

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.adaptation.sequence import sliding_windows, time_ordered_split, training_windows
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.core.frameworks import TORCH_FRAMEWORKS
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import TrainingSet

_KERAS_MODULES = frozenset({"keras", "tf_keras", "tensorflow"})


def _sequence_layers() -> tuple[type, ...]:
    from torch import nn

    return (
        nn.RNNBase,
        nn.Conv1d,
        nn.MultiheadAttention,
        nn.TransformerEncoder,
        nn.TransformerEncoderLayer,
    )


def is_torch_sequence(model: object) -> bool:
    """True for a torch module that reads windows of rows (see the module docstring)."""
    from torch import nn

    if not isinstance(model, nn.Module):
        return False
    if isinstance(getattr(model, "sequence_window", None), int):
        return True
    layers = _sequence_layers()
    return any(isinstance(m, layers) for m in model.modules())


def _torch_input_width(model: Any) -> int | None:
    from torch import nn

    declared = getattr(model, "n_features", None)
    if isinstance(declared, int):
        return declared
    for m in model.modules():
        if isinstance(m, nn.RNNBase):
            return int(m.input_size)
        if isinstance(m, nn.Conv1d):
            return int(m.in_channels)
        if isinstance(m, nn.TransformerEncoderLayer):
            return int(m.self_attn.embed_dim)
        if isinstance(m, nn.MultiheadAttention):
            return int(m.embed_dim)
        if isinstance(m, nn.Linear):
            return int(m.in_features)
    return None


def _estimator_type(
    declared: object, output_dim: int | None
) -> Literal["classifier", "regressor", "unknown"]:
    if declared == "classifier":
        return "classifier"
    if declared == "regressor":
        return "regressor"
    if output_dim is None:
        return "unknown"
    return "classifier" if output_dim > 1 else "regressor"


def _metrics(estimator_type: str, predictions: np.ndarray, y: np.ndarray) -> dict[str, float]:
    if estimator_type == "classifier":
        return {"accuracy": float(np.mean(predictions == y))}
    return {"rmse": float(np.sqrt(np.mean((predictions.astype(float) - y.astype(float)) ** 2)))}


def _save(model: object, artifact_dir: str) -> str:
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, "model.joblib")
    joblib.dump(model, path)
    return path


class _SequenceSettings:
    def __init__(self, settings: Settings) -> None:
        self.window = settings.sequence_window
        self.validation_fraction = settings.sequence_validation_fraction
        self.fine_tune_epochs = settings.sequence_fine_tune_epochs
        self.full_retrain_epochs = settings.sequence_full_retrain_epochs
        self.learning_rate = settings.sequence_learning_rate

    def epochs(self, strategy: Strategy) -> int:
        if strategy == Strategy.FINE_TUNING:
            return self.fine_tune_epochs
        return self.full_retrain_epochs


class TorchSequenceType:
    def __init__(self, settings: Settings) -> None:
        self.cfg = _SequenceSettings(settings)

    @property
    def frameworks(self) -> frozenset[str]:
        return TORCH_FRAMEWORKS

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return {
            Strategy.FINE_TUNING: "TORCH_SEQUENCE_FINE_TUNE",
            Strategy.FULL_RETRAINING: "TORCH_SEQUENCE_FULL_RETRAIN",
        }

    def accepts(self, model: object, framework: str) -> bool:
        return is_torch_sequence(model)

    def inspect(self, model: object, framework: str) -> ModelInspection:
        from torch import nn

        module: Any = model
        linears = [m for m in module.modules() if isinstance(m, nn.Linear)]
        output_dim = int(linears[-1].out_features) if linears else None
        declared = getattr(module, "sequence_window", None)
        return ModelInspection(
            framework="torch",
            model_class=type(model).__name__,
            estimator_type=_estimator_type(getattr(module, "estimator_type", None), output_dim),
            supports_warm_start=True,
            n_parameters=sum(p.numel() for p in module.parameters()),
            input_dim=_torch_input_width(module),
            output_dim=output_dim,
            temporal=True,
            sequence_window=declared if isinstance(declared, int) else self.cfg.window,
        )

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        import torch
        from torch import nn

        if strategy not in self.engines:
            raise UnsupportedAdaptationError(f"no {strategy.value} engine for sequence models")
        window = inspection.sequence_window or self.cfg.window
        classifier = inspection.estimator_type == "classifier"
        Xw, yw = training_windows(data.X.to_numpy(), data.y.to_numpy(), window)
        train, held = time_ordered_split(len(Xw), self.cfg.validation_fraction)
        inputs = torch.tensor(Xw, dtype=torch.float32)
        target = torch.tensor(yw, dtype=torch.long if classifier else torch.float32)

        fresh: Any = copy.deepcopy(model)
        if strategy == Strategy.FULL_RETRAINING:
            for m in fresh.modules():
                reset = getattr(m, "reset_parameters", None)
                if callable(reset) and m is not fresh:
                    reset()
        loss_fn: nn.Module = nn.CrossEntropyLoss() if classifier else nn.MSELoss()

        def loss(rows: range) -> torch.Tensor:
            out = fresh(inputs[rows.start : rows.stop])
            want = target[rows.start : rows.stop]
            return loss_fn(out, want) if classifier else loss_fn(out.reshape(-1), want)

        optimizer = torch.optim.Adam(fresh.parameters(), lr=self.cfg.learning_rate)
        best_loss, best_state = float("inf"), copy.deepcopy(fresh.state_dict())
        try:
            for _ in range(self.cfg.epochs(strategy)):
                fresh.train()
                optimizer.zero_grad()
                loss(train).backward()
                optimizer.step()
                fresh.eval()
                with torch.no_grad():
                    judged = float(loss(held if len(held) else train))
                if judged < best_loss:
                    best_loss, best_state = judged, copy.deepcopy(fresh.state_dict())
        except Exception as exc:
            raise ArtifactError(
                f"sequence training failed on {type(model).__name__}", cause=str(exc)
            ) from exc
        fresh.load_state_dict(best_state)
        fresh.eval()
        fresh.sequence_window = window

        stamped = inspection.model_copy(update={"sequence_window": window})
        predictions = self.predict(fresh, data.X, inspection=stamped)[window - 1 :]
        return CandidateModel(
            engine=self.engines[strategy],
            framework="torch",
            model_class=type(fresh).__name__,
            artifact_path=_save(fresh, data.artifact_dir),
            metrics=_metrics(inspection.estimator_type, predictions, yw),
            n_train_rows=len(data.X),
            feature_names=list(data.X.columns),
            target_column=data.target_column,
        )

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        import torch

        module: Any = model
        window = getattr(module, "sequence_window", None) or inspection.sequence_window
        windows = sliding_windows(X.to_numpy(), int(window or self.cfg.window))
        module.eval()
        with torch.no_grad():
            outputs = module(torch.tensor(windows, dtype=torch.float32))
        if inspection.estimator_type == "classifier":
            return np.asarray(outputs.argmax(dim=-1).numpy())
        return np.asarray(outputs.reshape(len(windows), -1)[:, 0].numpy())


class KerasType:
    def __init__(self, settings: Settings) -> None:
        self.cfg = _SequenceSettings(settings)

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset({"keras", "tensorflow"})

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return {
            Strategy.FINE_TUNING: "KERAS_FINE_TUNE",
            Strategy.FULL_RETRAINING: "KERAS_FULL_RETRAIN",
        }

    def accepts(self, model: object, framework: str) -> bool:
        root = type(model).__module__.split(".", 1)[0]
        return root in _KERAS_MODULES and all(
            hasattr(model, a) for a in ("fit", "predict", "input_shape", "output_shape")
        )

    def inspect(self, model: object, framework: str) -> ModelInspection:
        net: Any = model
        in_shape = tuple(net.input_shape)
        out_shape = tuple(net.output_shape)
        output_dim = out_shape[-1] if len(out_shape) > 1 and isinstance(out_shape[-1], int) else 1
        sequence = len(in_shape) == 3
        window = in_shape[1] if sequence and isinstance(in_shape[1], int) else self.cfg.window
        width = in_shape[-1] if isinstance(in_shape[-1], int) else None
        return ModelInspection(
            framework=framework,
            model_class=type(model).__name__,
            estimator_type=_estimator_type(getattr(net, "estimator_type", None), output_dim),
            supports_warm_start=getattr(net, "optimizer", None) is not None,
            n_parameters=int(net.count_params()),
            input_dim=width,
            output_dim=output_dim,
            temporal=sequence,
            sequence_window=window if sequence else None,
        )

    def _inputs(self, X: np.ndarray, inspection: ModelInspection) -> np.ndarray:
        if inspection.sequence_window:
            return sliding_windows(X, inspection.sequence_window)
        return np.asarray(X, dtype=np.float32)

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        net: Any = model
        if getattr(net, "optimizer", None) is None:
            raise UnsupportedAdaptationError(
                "the keras model is not compiled: there is no optimizer or loss to train with"
            )
        if strategy not in self.engines:
            raise UnsupportedAdaptationError(f"no {strategy.value} engine for keras models")
        X, y = data.X.to_numpy(), data.y.to_numpy()
        window = inspection.sequence_window
        if window:
            inputs, target = training_windows(X, y, window)
        else:
            inputs, target = np.asarray(X, dtype=np.float32), y
        train, held = time_ordered_split(len(inputs), self.cfg.validation_fraction)
        import keras

        fresh: Any = keras.models.clone_model(net)
        if strategy == Strategy.FINE_TUNING:
            fresh.set_weights(net.get_weights())
        optimizer = type(net.optimizer).from_config(net.optimizer.get_config())
        fresh.compile(optimizer=optimizer, loss=net.loss)
        validation = None
        if len(held):
            validation = (inputs[held.start : held.stop], target[held.start : held.stop])
        try:
            fresh.fit(
                inputs[train.start : train.stop],
                target[train.start : train.stop],
                epochs=self.cfg.epochs(strategy),
                verbose=0,
                shuffle=not window,
                validation_data=validation,
            )
        except Exception as exc:
            raise ArtifactError(
                f"keras training failed on {type(model).__name__}", cause=str(exc)
            ) from exc
        predictions = self.predict(fresh, data.X, inspection=inspection)
        if window:
            predictions = predictions[window - 1 :]
        return CandidateModel(
            engine=self.engines[strategy],
            framework=inspection.framework,
            model_class=type(fresh).__name__,
            artifact_path=_save(fresh, data.artifact_dir),
            metrics=_metrics(inspection.estimator_type, predictions, target),
            n_train_rows=len(data.X),
            feature_names=list(data.X.columns),
            target_column=data.target_column,
        )

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        net: Any = model
        outputs = np.asarray(net.predict(self._inputs(X.to_numpy(), inspection), verbose=0))
        per_class = outputs.ndim > 1 and outputs.shape[-1] > 1
        if inspection.estimator_type == "classifier" and per_class:
            return np.asarray(outputs.argmax(axis=-1))
        return outputs.reshape(len(X), -1)[:, 0]


_SEQUENCE_KEYS = (
    "sequence_window",
    "sequence_validation_fraction",
    "sequence_fine_tune_epochs",
    "sequence_full_retrain_epochs",
)

TORCH_SEQUENCE = AdapterSpec(
    capability=Capability(
        port="model_type",
        adapter="torch-sequence",
        description="torch RNN/LSTM/GRU, TCN and transformer sequence models over sliding windows",
        features=frozenset({"sequence", "temporal", "framework:torch", "framework:pytorch"}),
        config_keys=(*_SEQUENCE_KEYS, "sequence_learning_rate"),
        distributions=("torch",),
    ),
    factory=TorchSequenceType,
)
KERAS = AdapterSpec(
    capability=Capability(
        port="model_type",
        adapter="keras",
        description="compiled Keras models, sequence (rank-3 input, sliding windows) or tabular",
        features=frozenset({"sequence", "tabular", "temporal", "framework:keras",
                            "framework:tensorflow"}),
        config_keys=_SEQUENCE_KEYS,
        distributions=("keras",),
    ),
    factory=KerasType,
)
