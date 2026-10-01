"""Model type port: what one kind of model is, how it is adapted and how it predicts.

A model handler (``ModelHandlerPort``) turns a model into bytes and back. A model type plugin
(``ModelTypePort``, entry-point group ``oran_adapt.model_type``, docs/adapters/model_type.md)
knows what the loaded object *is*: it recognises it (``accepts``), describes it (``inspect``),
produces a retrained or fine-tuned candidate from it (``adapt``) and makes predictions with it
(``predict``). Everything specific to a kind of model - tabular or sequence input, windowing
and lags, continued boosting, refitting a classical forecaster, the order rows are split in -
lives in its plugin, so adding a kind of model is one installed plugin and no core change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

    from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
    from oran_adapt.core.enums import Strategy


@dataclass(frozen=True)
class TrainingSet:
    """What ``adapt`` trains on. Rows are in time order (oldest first) whenever the data has a
    time order: a temporal plugin must keep that order and never shuffle it. ``artifact_dir``
    is where the candidate is written (``CandidateModel.artifact_path``, a joblib file)."""

    X: pd.DataFrame
    y: pd.Series
    target_column: str
    artifact_dir: str


@runtime_checkable
class ModelTypePort(Protocol):
    """One kind of model. ``frameworks`` are the framework names (as recorded on the model)
    it may serve; ``engines`` maps each strategy it can carry out to the engine name recorded
    on the candidate; an empty mapping means the kind can be scored but not adapted.

    ``accepts`` and ``inspect`` must not raise for a model of another kind: ``accepts`` returns
    False. ``adapt`` raises UnsupportedAdaptationError when the loaded model cannot be adapted
    that way and ArtifactError when training fails. ``predict`` returns one prediction per row
    of ``X``, in order."""

    @property
    def frameworks(self) -> frozenset[str]: ...

    @property
    def engines(self) -> Mapping[Strategy, str]: ...

    def accepts(self, model: object, framework: str) -> bool: ...

    def inspect(self, model: object, framework: str) -> ModelInspection: ...

    def adapt(
        self,
        model: object,
        strategy: Strategy,
        *,
        inspection: ModelInspection,
        data: TrainingSet,
    ) -> CandidateModel: ...

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray: ...
