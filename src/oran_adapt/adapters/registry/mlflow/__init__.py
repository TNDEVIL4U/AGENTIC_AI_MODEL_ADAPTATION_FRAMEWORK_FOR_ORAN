"""Registry adapter ``mlflow`` (the reference adapter): models, versions, tags and aliases in
the MLflow model registry.

Artifacts go through the MLflow tracking server's own artifact store (``--serve-artifacts``,
volume-backed); there is no separate object store. A new version is one MLflow run holding the
artifact directory under ``model/``, its metrics, tags and training-data lineage, registered
from that run. The directory itself comes from a model handler; this adapter never builds or
loads a model object. Versions registered earlier with ``mlflow.<flavor>.log_model`` read and
download the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.adapters.registry.mlflow.registry import MlflowRegistry
    from oran_adapt.core.config import Settings


def _build(settings: Settings) -> MlflowRegistry:
    # Imported here so that discovering adapters (plugins.adapters) never loads the MLflow SDK.
    from oran_adapt.adapters.registry.mlflow.registry import MlflowRegistry

    return MlflowRegistry.from_settings(settings)


def __getattr__(name: str) -> Any:
    if name == "MlflowRegistry":
        from oran_adapt.adapters.registry.mlflow.registry import MlflowRegistry

        return MlflowRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="mlflow",
        description="MLflow model registry; artifacts via the tracking server's artifact store",
        features=frozenset({"aliases", "version_tags", "version_metrics", "lineage_inputs"}),
        config_keys=(
            "mlflow_tracking_uri",
            "mlflow_registry_uri",
            "registry_tags_checksum",
            "registry_tags_status",
            "artifact_max_bytes",
            "artifact_hash_chunk_bytes",
            "mlflow_http_max_retries",
            "mlflow_http_backoff_factor",
            "mlflow_http_timeout_s",
        ),
        required_keys=("mlflow_tracking_uri",),
        production_keys=("mlflow_tracking_uri",),
        distributions=("mlflow",),
    ),
    factory=_build,
)
