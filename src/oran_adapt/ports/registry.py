"""Model registry, artifact store and model handler ports.

A registry stores *opaque artifact directories* and the metadata around them (versions, tags,
aliases, metrics, lineage). It never builds or loads a model object: turning a model into a
directory and back is the model handler's job (``ModelHandlerPort``). A version is addressed
independently of the backend by a ``model://`` URI (``oran_adapt.core.model_uri``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.integrity import ArtifactPolicy

# ModelVersion.status values. A registry maps its own states onto these three.
READY = "READY"
PENDING = "PENDING"
FAILED = "FAILED"


@dataclass(frozen=True)
class ModelVersion:
    """One registered version, as every registry adapter reports it."""

    name: str
    version: str
    tags: Mapping[str, str] = field(default_factory=dict)
    status: str = READY
    created_at_ms: int | None = None
    # The backend's own handle on how the version was produced (an MLflow run id, a SageMaker
    # model package ARN, a Vertex model resource), for display and lineage only.
    source: str | None = None


@dataclass(frozen=True)
class RegisteredModel:
    name: str
    aliases: Mapping[str, str] = field(default_factory=dict)  # alias -> version


@runtime_checkable
class ModelRegistryPort(Protocol):
    """The single authority for registered models, their versions, tags and aliases.

    Versions are strings of a positive integer, numbered per model from "1" in creation order.
    Errors are the core's typed errors: ModelNotFoundError for a missing model, version or
    alias; RegistryUnavailableError when the backend fails. Adapters are picklable, so a job
    worker process can receive the resolved instance. ``artifact_policy`` holds the size limit,
    hash chunk and tag names every caller applies. ``oran_adapt.conformance.registry`` is the
    behaviour every adapter must pass.
    """

    artifact_policy: ArtifactPolicy

    def ping(self) -> None: ...

    def get_registered_model(self, name: str) -> RegisteredModel: ...

    def list_versions(self, name: str) -> list[ModelVersion]:
        """Every version of ``name``, oldest first. ModelNotFoundError if the model is absent."""
        ...

    def get_version(self, name: str, version: str) -> ModelVersion: ...

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        """The metrics recorded with the version when it was created (its training-time
        baseline); empty when none were recorded."""
        ...

    def get_version_by_alias(self, name: str, alias: str) -> str: ...

    def set_alias(self, name: str, alias: str, version: str) -> None:
        """Point ``alias`` at ``version``, moving it if it is set. ModelNotFoundError if the
        version does not exist."""
        ...

    def delete_alias(self, name: str, alias: str) -> None: ...

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        """Merge ``tags`` into the version's tags, overwriting keys that exist."""
        ...

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        """Copy the version's artifact directory under ``dst_path``; returns the local
        directory, whose files are byte-identical to what ``create_version`` stored."""
        ...

    def create_version(
        self,
        name: str,
        artifact_dir: str,
        *,
        metrics: dict[str, float] | None = None,
        tags: dict[str, str] | None = None,
        input_frame: pd.DataFrame | None = None,
        input_name: str | None = None,
        input_digest: str | None = None,
    ) -> str:
        """Store ``artifact_dir`` as the next version of ``name`` (creating the model if it is
        new) and return the version. ``input_frame`` is the training data, recorded as lineage
        (name, digest and schema - never the rows)."""
        ...


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Byte storage for artifacts addressed by a store-relative key ("a/b/c", no leading slash,
    no ".." segments)."""

    def put(self, key: str, local_path: str) -> str:
        """Store the file or directory at ``local_path`` under ``key``; returns its URI."""
        ...

    def get(self, key: str, dst_dir: str) -> str:
        """Fetch ``key`` into ``dst_dir``; returns the local path. ArtifactError if absent."""
        ...

    def exists(self, key: str) -> bool: ...


@runtime_checkable
class ModelHandlerPort(Protocol):
    """Turns native model objects of the frameworks it declares into an artifact directory in
    its ``format`` and back."""

    @property
    def frameworks(self) -> frozenset[str]: ...

    @property
    def format(self) -> str: ...

    def detect(self, local_path: str) -> bool:
        """Whether the directory at ``local_path`` holds an artifact in this handler's format."""
        ...

    def save(self, model: object, framework: str, dst_dir: str) -> str:
        """Write ``model`` into the new directory ``dst_dir``; returns it. ArtifactError when
        the model cannot be serialized safely, UnsupportedAdaptationError for an undeclared
        framework."""
        ...

    def load(self, local_path: str, framework: str) -> object:
        """The native model object stored at ``local_path``. ArtifactError when the directory
        does not hold a loadable model, UnsupportedAdaptationError for an undeclared framework."""
        ...
