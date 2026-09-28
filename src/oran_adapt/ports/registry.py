"""Model registry, artifact store and model handler ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.integrity import ArtifactPolicy


@runtime_checkable
class ModelRegistryPort(Protocol):
    """The single authority for registered models, their versions, tags and aliases.

    Errors are the core's typed errors: ModelNotFoundError for a missing model/version/alias,
    RegistryUnavailableError when the backend fails, ArtifactError for an artifact that cannot
    be stored or loaded safely, UnsupportedAdaptationError for a framework it cannot log.
    Adapters are picklable, so a job worker process can receive the resolved instance.
    ``artifact_policy`` holds the size limit, hash chunk and tag names every caller applies.
    """

    artifact_policy: ArtifactPolicy

    def ping(self) -> None: ...

    def get_registered_model(self, name: str) -> Any: ...

    def list_versions(self, name: str) -> list[Any]: ...

    def get_version(self, name: str, version: str) -> Any: ...

    def describe_versions(self, name: str) -> list[dict[str, Any]]: ...

    def get_run_metrics(self, run_id: str | None) -> dict[str, float]: ...

    def get_version_by_alias(self, name: str, alias: str) -> str: ...

    def set_alias(self, name: str, alias: str, version: str) -> None: ...

    def delete_alias(self, name: str, alias: str) -> None: ...

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None: ...

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str: ...

    def load_model(self, local_path: str, framework: str) -> object: ...

    def log_model(
        self,
        name: str,
        model: object,
        *,
        framework: str,
        metrics: dict[str, float] | None = None,
        tags: dict[str, str] | None = None,
        input_frame: pd.DataFrame | None = None,
        input_name: str | None = None,
        input_digest: str | None = None,
    ) -> str: ...

    def register_candidate(
        self,
        name: str,
        artifact_path: str,
        *,
        framework: str,
        metrics: dict[str, float],
        tags: dict[str, str] | None = None,
    ) -> str: ...

    def record_artifact_checksum(self, name: str, version: str, workdir: str) -> str: ...


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Byte storage for artifacts addressed by a store-relative key."""

    def put(self, key: str, local_path: str) -> str:
        """Store the file or directory at ``local_path`` under ``key``; returns its URI."""
        ...

    def get(self, key: str, dst_dir: str) -> str:
        """Fetch ``key`` into ``dst_dir``; returns the local path. ArtifactError if absent."""
        ...

    def exists(self, key: str) -> bool: ...


@runtime_checkable
class ModelHandlerPort(Protocol):
    """Loads (and later inspects/serializes) models of the frameworks it declares."""

    @property
    def frameworks(self) -> frozenset[str]: ...

    def load(self, local_path: str, framework: str) -> object:
        """The native model object stored at ``local_path``. ArtifactError when the directory
        does not hold a loadable model, UnsupportedAdaptationError for an undeclared framework."""
        ...
