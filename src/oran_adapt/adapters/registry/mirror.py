"""Registry adapter ``mirror``: one registry is the authority, a second follows it.

REGISTRY_MIRROR_PRIMARY and REGISTRY_MIRROR_REPLICA name two other registry adapters (for
example ``mlflow`` and ``filesystem`` during a migration, or two regions of one backend).
Every read goes to the primary. Every write goes to the primary first, then to the replica;
the replica numbers its versions independently, so each replica version carries the primary
version it copies in the ``oran.mirror.source_version`` tag. When a replica write fails,
REGISTRY_MIRROR_ON_REPLICA_ERROR decides: ``fail`` raises (the primary write has happened and
stays; ``sync`` repairs the replica) and ``log`` logs the divergence and carries on.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from oran_adapt.core.errors import ConfigurationError, ModelNotFoundError, RegistryUnavailableError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.core.integrity import ArtifactPolicy
    from oran_adapt.ports import ModelRegistryPort, ModelVersion, RegisteredModel

logger = logging.getLogger("oran_adapt.registry.mirror")

SOURCE_TAG = "oran.mirror.source_version"
_T = TypeVar("_T")


class MirrorRegistry:
    def __init__(
        self, primary: ModelRegistryPort, replica: ModelRegistryPort, *, on_replica_error: str
    ) -> None:
        if on_replica_error not in ("fail", "log"):
            raise ConfigurationError(
                "on_replica_error must be 'fail' or 'log'", value=on_replica_error
            )
        self.primary = primary
        self.replica = replica
        self.on_replica_error = on_replica_error

    @classmethod
    def from_settings(cls, settings: Settings) -> MirrorRegistry:
        from oran_adapt import plugins

        names = (settings.registry_mirror_primary, settings.registry_mirror_replica)
        if None in names or names[0] == names[1] or "mirror" in names:
            raise ConfigurationError(
                "REGISTRY_MIRROR_PRIMARY and REGISTRY_MIRROR_REPLICA must name two different "
                "registry adapters other than 'mirror'",
                primary=names[0],
                replica=names[1],
            )
        built = [
            cast(
                "ModelRegistryPort",
                plugins.resolve("registry", str(name), config_key=key).factory(settings),
            )
            for name, key in zip(
                names, ("registry_mirror_primary", "registry_mirror_replica"), strict=True
            )
        ]
        return cls(built[0], built[1], on_replica_error=settings.registry_mirror_on_replica_error)

    @property
    def artifact_policy(self) -> ArtifactPolicy:
        return self.primary.artifact_policy

    def _replicate(self, action: str, name: str, write: Callable[[], _T]) -> _T | None:
        try:
            return write()
        except Exception as exc:
            if self.on_replica_error == "fail":
                raise RegistryUnavailableError(
                    "the primary registry was updated but the replica was not; run sync",
                    action=action,
                    model=name,
                    cause=str(exc),
                ) from exc
            logger.warning(
                "registry replica diverged from the primary",
                extra={"action": action, "model": name, "cause": str(exc)},
            )
            return None

    def replica_version(self, name: str, version: str) -> str | None:
        """The replica version copying primary ``version``; None when it has not been copied."""
        try:
            versions = self.replica.list_versions(name)
        except ModelNotFoundError:
            return None
        for v in versions:
            if v.tags.get(SOURCE_TAG) == version:
                return v.version
        return None

    def _mapped(self, name: str, version: str) -> str:
        mapped = self.replica_version(name, version)
        if mapped is None:
            raise ModelNotFoundError(
                f"version '{version}' of '{name}' has not been copied to the replica",
                model=name,
                version=version,
            )
        return mapped

    # ---- reads: the primary ------------------------------------------------------------

    def ping(self) -> None:
        self.primary.ping()
        self._replicate("ping", "", self.replica.ping)

    def get_registered_model(self, name: str) -> RegisteredModel:
        return self.primary.get_registered_model(name)

    def list_versions(self, name: str) -> list[ModelVersion]:
        return self.primary.list_versions(name)

    def get_version(self, name: str, version: str) -> ModelVersion:
        return self.primary.get_version(name, version)

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        return self.primary.get_version_metrics(name, version)

    def get_version_by_alias(self, name: str, alias: str) -> str:
        return self.primary.get_version_by_alias(name, alias)

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        return self.primary.download_artifacts(name, version, dst_path)

    # ---- writes: primary, then replica -------------------------------------------------

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self.primary.set_alias(name, alias, version)
        self._replicate(
            "set_alias",
            name,
            lambda: self.replica.set_alias(name, alias, self._mapped(name, version)),
        )

    def delete_alias(self, name: str, alias: str) -> None:
        self.primary.delete_alias(name, alias)
        self._replicate("delete_alias", name, lambda: self.replica.delete_alias(name, alias))

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        self.primary.set_version_tags(name, version, tags)
        self._replicate(
            "set_version_tags",
            name,
            lambda: self.replica.set_version_tags(name, self._mapped(name, version), tags),
        )

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
        version = self.primary.create_version(
            name,
            artifact_dir,
            metrics=metrics,
            tags=tags,
            input_frame=input_frame,
            input_name=input_name,
            input_digest=input_digest,
        )
        self._replicate(
            "create_version",
            name,
            lambda: self.replica.create_version(
                name,
                artifact_dir,
                metrics=metrics,
                tags={**(tags or {}), SOURCE_TAG: version},
                input_frame=input_frame,
                input_name=input_name,
                input_digest=input_digest,
            ),
        )
        return version

    def sync(self, name: str, workdir: str | None = None) -> dict[str, Any]:
        """Bring the replica's copy of ``name`` level with the primary: copy missing versions
        (artifacts, metrics, tags), then overwrite tags and aliases. Returns what changed."""
        copied: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mirror-sync-", dir=workdir) as tmp:
            for v in self.primary.list_versions(name):
                if self.replica_version(name, v.version) is not None:
                    continue
                local = self.primary.download_artifacts(
                    name, v.version, os.path.join(tmp, v.version)
                )
                self.replica.create_version(
                    name,
                    local,
                    metrics=self.primary.get_version_metrics(name, v.version),
                    tags={**v.tags, SOURCE_TAG: v.version},
                )
                copied.append(v.version)
        for v in self.primary.list_versions(name):
            self.replica.set_version_tags(name, self._mapped(name, v.version), dict(v.tags))
        primary_aliases = self.primary.get_registered_model(name).aliases
        for alias, version in primary_aliases.items():
            self.replica.set_alias(name, alias, self._mapped(name, version))
        removed = [
            alias
            for alias in self.replica.get_registered_model(name).aliases
            if alias not in primary_aliases
        ]
        for alias in removed:
            self.replica.delete_alias(name, alias)
        return {"copied_versions": copied, "aliases": dict(primary_aliases), "removed": removed}


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="mirror",
        description="primary registry with a replica that follows every write",
        features=frozenset({"aliases", "version_tags", "version_metrics", "sync"}),
        config_keys=(
            "registry_mirror_primary",
            "registry_mirror_replica",
            "registry_mirror_on_replica_error",
        ),
        required_keys=("registry_mirror_primary", "registry_mirror_replica"),
    ),
    factory=MirrorRegistry.from_settings,
)
