"""Model handler adapter ``mlflow-flavors``: loads the native model object (an sklearn or
xgboost estimator, a torch.nn.Module) from a downloaded MLflow model directory, so the inspector
looks at it directly instead of through MLflow's generic pyfunc wrapper. Also owns the skops
trust check MLflow >= 3 applies when it serializes sklearn models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

# Framework name -> MLflow flavor module. "pytorch" is accepted as an alias of "torch".
FLAVORS: dict[str, str] = {
    "sklearn": "mlflow.sklearn",
    "xgboost": "mlflow.xgboost",
    "torch": "mlflow.pytorch",
    "pytorch": "mlflow.pytorch",
}


def resolve_skops_trusted_types(model: object, allowed: tuple[str, ...] | list[str]) -> list[str]:
    """The skops types ``model`` needs trusted to be saved by ``mlflow.sklearn``. Raises
    ArtifactError - a deterministic failure, never retried - when it needs any type outside
    ``allowed``, rather than letting MLflow fail later with a generic error that looks like an
    outage."""
    import skops.io as sio

    try:
        needed = sio.get_untrusted_types(data=sio.dumps(model))
    except Exception as exc:
        raise ArtifactError(
            f"could not serialize {type(model).__name__} with skops", cause=str(exc)
        ) from exc
    refused = sorted(set(needed) - set(allowed))
    if refused:
        raise ArtifactError(
            f"{type(model).__name__} needs skops types that are not on the trusted list",
            untrusted_types=refused,
            hint="review them, then add to MLFLOW_SKOPS_TRUSTED_TYPES",
        )
    return sorted(needed)


class MlflowFlavorHandler:
    """Loads models saved by ``mlflow.<flavor>.log_model``."""

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset(FLAVORS)

    def load(self, local_path: str, framework: str) -> object:
        """``local_path`` is a local directory containing an MLmodel file, as produced by the
        registry's ``download_artifacts``."""
        import importlib

        flavor = FLAVORS.get(framework.lower())
        if flavor is None:
            raise UnsupportedAdaptationError(f"no model loader for framework {framework!r}")
        try:
            return importlib.import_module(flavor).load_model(local_path)
        except Exception as exc:
            raise ArtifactError(
                f"failed to load {framework} model artifact", path=local_path, cause=str(exc)
            ) from exc


def _build(settings: Settings) -> MlflowFlavorHandler:
    return MlflowFlavorHandler()


SPEC = AdapterSpec(
    capability=Capability(
        port="model_handler",
        adapter="mlflow-flavors",
        description="sklearn, xgboost and torch models stored in MLflow's flavor format",
        features=frozenset({"load", *FLAVORS}),
        distributions=("mlflow",),
    ),
    factory=_build,
)
