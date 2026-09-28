"""Artifact store adapters (port ``artifact_store``): ``filesystem`` and ``fsspec``.

``filesystem`` keeps artifacts under ARTIFACT_STORE_ROOT (a local disk or a shared volume) and
publishes each one with an atomic rename, so a reader never sees half an artifact. ``fsspec``
is the object-store adapter: any URL fsspec understands (``s3://`` with s3fs, ``gs://`` with
gcsfs, ``abfs://`` with adlfs, ``memory://`` for tests). Files are copied one by one so the
layout is the same on every backend.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import ArtifactError, RegistryUnavailableError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def check_key(key: str) -> str:
    """``key`` if it is a safe store-relative key; ArtifactError otherwise."""
    parts = key.split("/")
    if not key or key.startswith("/") or "\\" in key or any(p in ("", ".", "..") for p in parts):
        raise ArtifactError(f"invalid artifact key {key!r}", key=key)
    return key


class FilesystemArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *check_key(key).split("/"))

    def put(self, key: str, local_path: str) -> str:
        target = self._path(key)
        if os.path.exists(target):
            raise ArtifactError(f"artifact key {key!r} already exists", key=key)
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        staging = os.path.join(parent, f".staging-{uuid.uuid4().hex}")
        try:
            if os.path.isdir(local_path):
                shutil.copytree(local_path, staging)
            else:
                shutil.copy2(local_path, staging)
            os.replace(staging, target)
        except OSError as exc:
            raise RegistryUnavailableError(
                "could not write to the artifact store", key=key, cause=str(exc)
            ) from exc
        return Path(target).as_uri()

    def get(self, key: str, dst_dir: str) -> str:
        source = self._path(key)
        if not os.path.exists(source):
            raise ArtifactError(f"artifact {key!r} is not in the store", key=key)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(source))
        try:
            if os.path.isdir(source):
                shutil.copytree(source, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(source, dst)
        except OSError as exc:
            raise RegistryUnavailableError(
                "could not read from the artifact store", key=key, cause=str(exc)
            ) from exc
        return dst

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))


class FsspecArtifactStore:
    def __init__(self, url: str) -> None:
        import fsspec

        self.url = url.rstrip("/")
        self.fs, self.base = fsspec.core.url_to_fs(self.url)

    def __getstate__(self) -> dict[str, Any]:
        return {"url": self.url}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(state["url"])  # type: ignore[misc]

    def _remote(self, key: str) -> str:
        return f"{self.base}/{check_key(key)}"

    def put(self, key: str, local_path: str) -> str:
        target = self._remote(key)
        try:
            if self.fs.exists(target):
                raise ArtifactError(f"artifact key {key!r} already exists", key=key)
            if os.path.isdir(local_path):
                for root, _, files in os.walk(local_path):
                    for name in files:
                        rel = os.path.relpath(os.path.join(root, name), local_path)
                        remote = f"{target}/{rel.replace(os.sep, '/')}"
                        self.fs.makedirs(remote.rsplit("/", 1)[0], exist_ok=True)
                        self.fs.put_file(os.path.join(root, name), remote)
            else:
                self.fs.makedirs(target.rsplit("/", 1)[0], exist_ok=True)
                self.fs.put_file(local_path, target)
        except ArtifactError:
            raise
        except Exception as exc:
            raise RegistryUnavailableError(
                "could not write to the object store", key=key, cause=str(exc)
            ) from exc
        return str(self.fs.unstrip_protocol(target))

    def get(self, key: str, dst_dir: str) -> str:
        source = self._remote(key)
        dst = os.path.join(dst_dir, key.rsplit("/", 1)[-1])
        try:
            if not self.fs.exists(source):
                raise ArtifactError(f"artifact {key!r} is not in the store", key=key)
            if self.fs.isdir(source):
                for remote in self.fs.find(source):
                    rel = remote[len(source) :].lstrip("/")
                    local = os.path.join(dst, *rel.split("/"))
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    self.fs.get_file(remote, local)
            else:
                os.makedirs(dst_dir, exist_ok=True)
                self.fs.get_file(source, dst)
        except ArtifactError:
            raise
        except Exception as exc:
            raise RegistryUnavailableError(
                "could not read from the object store", key=key, cause=str(exc)
            ) from exc
        return dst

    def exists(self, key: str) -> bool:
        try:
            return bool(self.fs.exists(self._remote(key)))
        except Exception as exc:
            raise RegistryUnavailableError(
                "could not reach the object store", key=key, cause=str(exc)
            ) from exc


def _filesystem(settings: Settings) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(settings.artifact_store_root)


def _fsspec(settings: Settings) -> FsspecArtifactStore:
    return FsspecArtifactStore(str(settings.artifact_store_url))


FILESYSTEM = AdapterSpec(
    capability=Capability(
        port="artifact_store",
        adapter="filesystem",
        description="artifacts under a local or shared directory, published by atomic rename",
        features=frozenset({"atomic_put"}),
        config_keys=("artifact_store_root",),
        production_keys=("artifact_store_root",),
    ),
    factory=_filesystem,
)

FSSPEC = AdapterSpec(
    capability=Capability(
        port="artifact_store",
        adapter="fsspec",
        description="object store by fsspec URL (s3://, gs://, abfs://, memory://)",
        features=frozenset({"object_store"}),
        config_keys=("artifact_store_url",),
        required_keys=("artifact_store_url",),
        distributions=("fsspec",),
    ),
    factory=_fsspec,
)
