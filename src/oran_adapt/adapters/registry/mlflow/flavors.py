"""Model handler adapter ``mlflow-flavors``: MLflow's flavor format (a directory with an
``MLmodel`` file). Loads the native model object (an sklearn or xgboost estimator, a
torch.nn.Module) directly instead of through MLflow's generic pyfunc wrapper, and saves new
versions in the same format so they stay loadable with ``mlflow.<flavor>.load_model``."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from oran_adapt.adapters.handlers.native import resolve_skops_trusted_types
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

FORMAT = "mlflow-flavor"
# Framework name -> MLflow flavor module. "pytorch" is accepted as an alias of "torch".
FLAVORS: dict[str, str] = {
    "sklearn": "mlflow.sklearn",
    "xgboost": "mlflow.xgboost",
    "torch": "mlflow.pytorch",
    "pytorch": "mlflow.pytorch",
}


def _flavor(framework: str) -> Any:
    import importlib

    name = FLAVORS.get(framework.lower())
    if name is None:
        raise UnsupportedAdaptationError(f"no MLflow flavor for framework {framework!r}")
    return importlib.import_module(name)


class MlflowFlavorHandler:
    def __init__(self, skops_trusted_types: Sequence[str]) -> None:
        self.skops_trusted_types = tuple(skops_trusted_types)

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset(FLAVORS)

    @property
    def format(self) -> str:
        return FORMAT

    def detect(self, local_path: str) -> bool:
        return os.path.isfile(os.path.join(local_path, "MLmodel"))

    def save(self, model: object, framework: str, dst_dir: str) -> str:
        flavor = _flavor(framework)
        extra: dict[str, Any] = {}
        if flavor.__name__ == "mlflow.sklearn":
            trusted = resolve_skops_trusted_types(model, self.skops_trusted_types)
            extra["skops_trusted_types"] = trusted or None
        elif flavor.__name__ == "mlflow.pytorch":
            extra["serialization_format"] = "pickle"
        try:
            flavor.save_model(model, dst_dir, **extra)
        except Exception as exc:
            raise ArtifactError(
                f"could not save the {framework} model in MLflow's format",
                path=dst_dir,
                cause=str(exc),
            ) from exc
        return dst_dir

    def load(self, local_path: str, framework: str) -> object:
        """``local_path`` is a local directory containing an MLmodel file, as produced by a
        registry's ``download_artifacts``."""
        flavor = _flavor(framework)
        try:
            return flavor.load_model(local_path)
        except Exception as exc:
            raise ArtifactError(
                f"failed to load {framework} model artifact", path=local_path, cause=str(exc)
            ) from exc


def _build(settings: Settings) -> MlflowFlavorHandler:
    return MlflowFlavorHandler(settings.mlflow_skops_trusted_types)


SPEC = AdapterSpec(
    capability=Capability(
        port="model_handler",
        adapter="mlflow-flavors",
        description="sklearn, xgboost and torch models stored in MLflow's flavor format",
        features=frozenset({"load", "save", *FLAVORS}),
        config_keys=("mlflow_skops_trusted_types",),
        distributions=("mlflow",),
    ),
    factory=_build,
)
