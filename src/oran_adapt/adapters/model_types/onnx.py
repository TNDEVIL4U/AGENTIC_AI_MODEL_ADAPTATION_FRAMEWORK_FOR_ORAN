"""Model type plugin ``onnx``: an ONNX graph (``onnx.ModelProto``).

ONNX is an inference format: a graph holds no training procedure, so this plugin inspects and
scores ONNX models but declares no engine - adapting one is refused with a typed
ADAPTATION_UNSUPPORTED (or handed to the LLM adapter when one is configured). Retrain the source
model and export it again instead.

Inspection reads the graph: the first input's last dimension is the feature width, an output
named like ``label`` (the skl2onnx classifier convention) makes it a classifier, a single value
per row a regressor. Predictions run on ``onnxruntime`` (ONNX_RUNTIME=onnxruntime, the
default) or on the ``onnx`` package's reference evaluator (ONNX_RUNTIME=reference: slow, no
extra dependency, meant for small models and tests). Inputs are fed as float32.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np

from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import TrainingSet


def _dims(value_info: Any) -> list[int | None]:
    shape = value_info.type.tensor_type.shape
    return [d.dim_value if d.HasField("dim_value") else None for d in shape.dim]


def _label_output(graph: Any) -> str | None:
    for out in graph.output:
        if "label" in out.name.lower():
            return str(out.name)
    return None


class OnnxType:
    def __init__(self, runtime: str) -> None:
        self.runtime = runtime

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset({"onnx"})

    @property
    def engines(self) -> Mapping[Strategy, str]:
        return {}

    def accepts(self, model: object, framework: str) -> bool:
        cls = type(model)
        return cls.__name__ == "ModelProto" and cls.__module__.split(".", 1)[0] == "onnx"

    def inspect(self, model: object, framework: str) -> ModelInspection:
        from onnx import numpy_helper

        proto: Any = model
        graph = proto.graph
        if not graph.input or not graph.output:
            raise ArtifactError("the ONNX graph has no input or no output")
        in_dims = _dims(graph.input[0])
        out_dims = _dims(graph.output[0])
        width = in_dims[-1] if len(in_dims) >= 2 else None
        per_row = out_dims[-1] if len(out_dims) >= 2 else 1
        if _label_output(graph) is not None:
            estimator_type = "classifier"
        elif per_row == 1:
            estimator_type = "regressor"
        else:
            estimator_type = "unknown"
        n_parameters = sum(int(numpy_helper.to_array(t).size) for t in graph.initializer)
        return ModelInspection(
            framework="onnx",
            model_class=graph.name or "ModelProto",
            estimator_type=estimator_type,
            n_features_in=width,
            input_dim=width,
            output_dim=per_row,
            n_parameters=n_parameters,
        )

    def adapt(
        self, model: object, strategy: Strategy, *, inspection: ModelInspection, data: TrainingSet
    ) -> CandidateModel:
        raise UnsupportedAdaptationError(
            "an ONNX graph cannot be retrained: retrain the source model and export it again"
        )

    def _session(self, proto: Any) -> tuple[Any, str]:
        if self.runtime == "reference":
            from onnx.reference import ReferenceEvaluator

            return ReferenceEvaluator(proto), "reference"
        import onnxruntime

        session = onnxruntime.InferenceSession(
            proto.SerializeToString(), providers=["CPUExecutionProvider"]
        )
        return session, "onnxruntime"

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        proto: Any = model
        graph = proto.graph
        session, _ = self._session(proto)
        feed = {graph.input[0].name: X.to_numpy(dtype=np.float32)}
        wanted = _label_output(graph) if inspection.estimator_type == "classifier" else None
        name = wanted or graph.output[0].name
        (outputs,) = session.run([name], feed)
        result = np.asarray(outputs)
        if inspection.estimator_type == "classifier" and wanted is None and result.ndim > 1:
            return np.asarray(result.argmax(axis=-1))
        return result.reshape(len(X), -1)[:, 0] if result.ndim > 1 else result


def _build(settings: Settings) -> OnnxType:
    return OnnxType(settings.onnx_runtime)


SPEC = AdapterSpec(
    capability=Capability(
        port="model_type",
        adapter="onnx",
        description="ONNX graphs: inspected and scored, never retrained",
        features=frozenset({"tabular", "inference-only", "framework:onnx"}),
        config_keys=("onnx_runtime",),
        distributions=("onnx", "onnxruntime"),
    ),
    factory=_build,
)
