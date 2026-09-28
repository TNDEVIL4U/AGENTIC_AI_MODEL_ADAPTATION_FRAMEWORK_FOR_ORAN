"""Registry adapter ``filesystem``: the model registry as JSON files, artifacts in any
``artifact_store`` adapter.

Layout under REGISTRY_FS_ROOT::

    models/<name>/model.json                  {"name", "aliases": {alias: version}, "next"}
    models/<name>/versions/<v>/version.json   tags, metrics, status, created_at_ms, lineage,
                                              artifact_key

Metadata writes take a per-model lock file (created with O_CREAT|O_EXCL, so it works on local
disks and on shared volumes that honour exclusive create) and replace files atomically. The
artifact upload happens outside the lock: the version is reserved as PENDING, uploaded, then
marked READY. Several replicas share one registry only through a shared volume.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from oran_adapt.core.errors import ArtifactError, ModelNotFoundError, RegistryUnavailableError
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.ports import AdapterSpec, Capability
from oran_adapt.ports.registry import FAILED, PENDING, READY, ModelVersion, RegisteredModel

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import ArtifactStorePort

_LOCK_POLL_S = 0.02


class FilesystemRegistry:
    def __init__(
        self,
        root: str,
        store: ArtifactStorePort,
        *,
        artifact_policy: ArtifactPolicy,
        lock_timeout_s: float,
    ) -> None:
        self.root = os.path.abspath(root)
        self.store = store
        self.artifact_policy = artifact_policy
        self.lock_timeout_s = lock_timeout_s

    @classmethod
    def from_settings(cls, settings: Settings) -> FilesystemRegistry:
        from oran_adapt import plugins

        spec = plugins.resolve(
            "artifact_store", settings.artifact_store_backend, config_key="artifact_store_backend"
        )
        store = cast("ArtifactStorePort", spec.factory(settings))
        return cls(
            settings.registry_fs_root,
            store,
            artifact_policy=ArtifactPolicy.from_settings(settings),
            lock_timeout_s=settings.registry_fs_lock_timeout_s,
        )

    # ---- files -------------------------------------------------------------------------

    def _model_dir(self, name: str) -> str:
        return os.path.join(self.root, "models", check_model_name(name))

    def _version_file(self, name: str, version: str) -> str:
        return os.path.join(self._model_dir(name), "versions", version, "version.json")

    @staticmethod
    def _read(path: str) -> dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise RegistryUnavailableError(
                "could not read registry metadata", path=path, cause=str(exc)
            ) from exc
        return data

    @staticmethod
    def _write(path: str, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise RegistryUnavailableError(
                "could not write registry metadata", path=path, cause=str(exc)
            ) from exc

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        model_dir = self._model_dir(name)
        os.makedirs(model_dir, exist_ok=True)
        path = os.path.join(model_dir, ".lock")
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RegistryUnavailableError(
                        "timed out waiting for the registry lock; if no other process holds "
                        "it, a crashed writer left it behind and it can be removed",
                        lock=path,
                        timeout_s=self.lock_timeout_s,
                    ) from None
                time.sleep(_LOCK_POLL_S)
            except OSError as exc:
                raise RegistryUnavailableError(
                    "could not take the registry lock", lock=path, cause=str(exc)
                ) from exc
        try:
            os.write(fd, f"{socket.gethostname()} {os.getpid()} {time.time()}".encode())
            os.close(fd)
            yield
        finally:
            os.remove(path)

    def _model(self, name: str) -> dict[str, Any]:
        try:
            return self._read(os.path.join(self._model_dir(name), "model.json"))
        except FileNotFoundError:
            raise ModelNotFoundError(f"Model '{name}' not found", model=name) from None

    def _save_model(self, name: str, data: dict[str, Any]) -> None:
        self._write(os.path.join(self._model_dir(name), "model.json"), data)

    def _raw_version(self, name: str, version: str) -> dict[str, Any]:
        self._model(name)
        if not version.isdigit():
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            )
        try:
            return self._read(self._version_file(name, version))
        except FileNotFoundError:
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            ) from None

    @staticmethod
    def _version(data: dict[str, Any]) -> ModelVersion:
        return ModelVersion(
            name=data["name"],
            version=data["version"],
            tags=dict(data.get("tags", {})),
            status=data["status"],
            created_at_ms=data.get("created_at_ms"),
            source=data.get("artifact_uri"),
        )

    # ---- port --------------------------------------------------------------------------

    def ping(self) -> None:
        try:
            os.makedirs(os.path.join(self.root, "models"), exist_ok=True)
            probe = os.path.join(self.root, f".ping-{uuid.uuid4().hex}")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            raise RegistryUnavailableError(
                "the registry directory is not writable", root=self.root, cause=str(exc)
            ) from exc

    def get_registered_model(self, name: str) -> RegisteredModel:
        return RegisteredModel(name=name, aliases=dict(self._model(name)["aliases"]))

    def list_versions(self, name: str) -> list[ModelVersion]:
        self._model(name)
        versions_dir = os.path.join(self._model_dir(name), "versions")
        found: list[ModelVersion] = []
        if os.path.isdir(versions_dir):
            for entry in os.listdir(versions_dir):
                path = os.path.join(versions_dir, entry, "version.json")
                if entry.isdigit() and os.path.isfile(path):
                    found.append(self._version(self._read(path)))
        return sorted(found, key=lambda v: int(v.version))

    def get_version(self, name: str, version: str) -> ModelVersion:
        return self._version(self._raw_version(name, version))

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        return {k: float(v) for k, v in self._raw_version(name, version)["metrics"].items()}

    def get_version_by_alias(self, name: str, alias: str) -> str:
        version = self._model(name)["aliases"].get(alias)
        if version is None:
            raise ModelNotFoundError(
                f"model '{name}' has no version aliased '{alias}'", model=name, alias=alias
            )
        return str(version)

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._raw_version(name, version)
        with self._lock(name):
            model = self._model(name)
            model["aliases"][alias] = version
            self._save_model(name, model)

    def delete_alias(self, name: str, alias: str) -> None:
        if alias not in self._model(name)["aliases"]:
            return
        with self._lock(name):
            model = self._model(name)
            if model["aliases"].pop(alias, None) is not None:
                self._save_model(name, model)

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        self._raw_version(name, version)
        with self._lock(name):
            data = self._raw_version(name, version)
            data["tags"].update({k: str(v) for k, v in tags.items()})
            self._write(self._version_file(name, version), data)

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        data = self._raw_version(name, version)
        if data["status"] != READY:
            raise ArtifactError(
                f"Model '{name}' version '{version}' has no stored artifact",
                model=name,
                version=version,
                status=data["status"],
            )
        return self.store.get(data["artifact_key"], dst_path)

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
        check_model_name(name)
        lineage: dict[str, Any] | None = None
        if input_frame is not None:
            lineage = {
                "name": input_name,
                "digest": input_digest,
                "schema": {str(c): str(t) for c, t in input_frame.dtypes.items()},
                "rows": len(input_frame),
            }
        with self._lock(name):
            try:
                model = self._model(name)
            except ModelNotFoundError:
                model = {"name": name, "aliases": {}, "next": 1}
            version = str(model["next"])
            model["next"] += 1
            key = f"models/{name}/{version}/{uuid.uuid4().hex}/model"
            record = {
                "name": name,
                "version": version,
                "tags": {k: str(v) for k, v in (tags or {}).items()},
                "metrics": {k: float(v) for k, v in (metrics or {}).items()},
                "status": PENDING,
                "created_at_ms": int(time.time() * 1000),
                "lineage": lineage,
                "artifact_key": key,
                "artifact_uri": None,
            }
            self._save_model(name, model)
            self._write(self._version_file(name, version), record)
        try:
            uri = self.store.put(key, artifact_dir)
        except Exception:
            self._finish(name, version, status=FAILED, uri=None)
            raise
        self._finish(name, version, status=READY, uri=uri)
        return version

    def _finish(self, name: str, version: str, *, status: str, uri: str | None) -> None:
        with self._lock(name):
            data = self._raw_version(name, version)
            data["status"] = status
            data["artifact_uri"] = uri
            self._write(self._version_file(name, version), data)


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="filesystem",
        description="registry metadata as JSON files; artifacts in the artifact_store adapter",
        features=frozenset({"aliases", "version_tags", "version_metrics", "lineage_inputs"}),
        config_keys=(
            "registry_fs_root",
            "registry_fs_lock_timeout_s",
            "artifact_store_backend",
            "registry_tags_checksum",
            "registry_tags_status",
            "artifact_max_bytes",
            "artifact_hash_chunk_bytes",
        ),
        production_keys=("registry_fs_root",),
    ),
    factory=FilesystemRegistry.from_settings,
)
