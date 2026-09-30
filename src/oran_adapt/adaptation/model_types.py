"""Every installed model type plugin behind one registry (docs/adapters/model_type.md).

The pipeline, validation, the reuse check and the decision layer ask this registry - never a
table of framework names - what a loaded model is, whether it can be adapted and how it
predicts. A plugin is chosen per model: the first of MODEL_TYPES (every installed plugin, by
name, when empty) that serves the model's framework and ``accepts`` the loaded object.

Inspection never crashes: a model no plugin recognises, or one whose plugin fails to describe
it, gives a typed UnsupportedModelType result (``inspect``), or UnsupportedModelTypeError with
that result as its context (``require``) - an error code and a reason, no stack trace.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, cast

from oran_adapt.adaptation.schemas import (
    CandidateModel,
    CapabilityAssessment,
    ModelInspection,
    UnsupportedModelType,
)
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import (
    ConfigurationError,
    UnsupportedAdaptationError,
    UnsupportedModelTypeError,
)

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import ModelTypePort, TrainingSet

logger = logging.getLogger(__name__)


class ModelTypes:
    """The model type plugins in the order they are tried."""

    def __init__(self, types: Mapping[str, ModelTypePort]) -> None:
        self.types = dict(types)

    @property
    def installed(self) -> dict[str, list[str]]:
        """Plugin name -> the frameworks it serves."""
        return {name: sorted(t.frameworks) for name, t in self.types.items()}

    def frameworks(self, strategy: Strategy | None = None) -> frozenset[str]:
        """Frameworks some plugin can adapt with ``strategy`` (with any strategy when None)."""
        found: set[str] = set()
        for port in self.types.values():
            engines = port.engines
            if (strategy is None and engines) or (strategy is not None and strategy in engines):
                found.update(f.lower() for f in port.frameworks)
        return frozenset(found)

    def _unsupported(self, model: object, framework: str, reason: str) -> UnsupportedModelType:
        return UnsupportedModelType(
            framework=framework,
            model_class=type(model).__name__,
            reason=reason,
            installed=self.installed,
        )

    def resolve(
        self, model: object, framework: str
    ) -> tuple[str, ModelTypePort] | UnsupportedModelType:
        fw = framework.lower()
        serving = [
            (name, port)
            for name, port in self.types.items()
            if fw in {f.lower() for f in port.frameworks}
        ]
        if not serving:
            return self._unsupported(
                model, framework, f"no installed model type plugin serves framework {framework!r}"
            )
        for name, port in serving:
            try:
                if port.accepts(model, fw):
                    return name, port
            except Exception as exc:  # noqa: BLE001 - a plugin that cannot tell means "not mine"
                logger.warning("model type %r failed to recognise %s: %s",
                               name, type(model).__name__, exc)
        return self._unsupported(
            model,
            framework,
            f"{type(model).__name__} is not a kind of {framework} model the installed plugins "
            f"handle (tried: {', '.join(name for name, _ in serving)})",
        )

    def inspect(self, model: object, framework: str) -> ModelInspection | UnsupportedModelType:
        """What the loaded model is, or the typed reason no plugin can say."""
        found = self.resolve(model, framework)
        if isinstance(found, UnsupportedModelType):
            return found
        name, port = found
        try:
            inspection = port.inspect(model, framework.lower())
        except Exception as exc:  # noqa: BLE001 - reported as a typed result, never a crash
            return self._unsupported(
                model, framework, f"model type {name!r} could not describe the model: {exc}"
            )
        return inspection.model_copy(update={"model_type": name})

    def require(self, model: object, framework: str) -> ModelInspection:
        """``inspect``, raising UnsupportedModelTypeError for an unsupported model."""
        result = self.inspect(model, framework)
        if isinstance(result, UnsupportedModelType):
            raise UnsupportedModelTypeError(result.reason, **result.model_dump(exclude={"reason"}))
        return result

    def _port(self, inspection: ModelInspection) -> ModelTypePort:
        name = inspection.model_type
        if name is None or name not in self.types:
            raise UnsupportedModelTypeError(
                f"model type {name!r} is not installed", installed=self.installed
            )
        return self.types[name]

    def adapt(
        self,
        model: object,
        strategy: Strategy,
        *,
        inspection: ModelInspection,
        capability: CapabilityAssessment,
        data: TrainingSet,
    ) -> CandidateModel:
        """A candidate produced by the model's plugin. UnsupportedAdaptationError when the
        plugin has no engine for ``strategy`` or the artifact cannot support it."""
        port = self._port(inspection)
        if strategy not in port.engines:
            raise UnsupportedAdaptationError(
                f"model type {inspection.model_type!r} has no {strategy.value} engine",
                engines=sorted(s.value for s in port.engines),
            )
        if strategy == Strategy.FINE_TUNING and not capability.supports_fine_tuning:
            raise UnsupportedAdaptationError(
                f"{inspection.framework} artifact has no fine-tuning capability: "
                f"{capability.reason}"
            )
        if not capability.supports_full_retraining:
            raise UnsupportedAdaptationError(
                f"{inspection.framework} artifact cannot be retrained: {capability.reason}"
            )
        return port.adapt(model, strategy, inspection=inspection, data=data)

    def predict(
        self, model: object, X: pd.DataFrame, *, inspection: ModelInspection
    ) -> np.ndarray:
        return self._port(inspection).predict(model, X, inspection=inspection)


def build_model_types(settings: Settings) -> ModelTypes:
    """The plugins MODEL_TYPES names, in that order; every installed one, by name, when it is
    empty."""
    from oran_adapt import plugins

    installed = plugins.adapters("model_type")
    names = list(settings.model_types) or sorted(installed)
    missing = [n for n in names if n not in installed]
    if missing:
        raise ConfigurationError(
            f"MODEL_TYPES names model type plugins that are not installed: {missing}",
            key="MODEL_TYPES",
            available=sorted(installed),
        )
    return ModelTypes(
        {name: cast("ModelTypePort", installed[name].factory(settings)) for name in names}
    )


@lru_cache(maxsize=1)
def default_model_types() -> ModelTypes:
    """The registry for the process settings, for callers that are not handed one."""
    from oran_adapt.core.config import get_settings

    return build_model_types(get_settings())


def supported_frameworks(settings: Settings, types: ModelTypes | None = None) -> frozenset[str]:
    """DECISION_SUPPORTED_FRAMEWORKS, or every framework an installed plugin can adapt when
    that is empty."""
    configured = frozenset(f.lower() for f in settings.decision_supported_frameworks)
    if configured:
        return configured
    return (types or build_model_types(settings)).frameworks()
