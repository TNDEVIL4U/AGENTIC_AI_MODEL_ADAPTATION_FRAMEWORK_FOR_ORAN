"""``{{ cookiecutter.class_name }}``: the ``ModelRegistryPort`` implementation.

As generated, this is a complete, single-writer registry over a local directory: metadata in one
JSON index, artifacts copied under ``artifacts/<name>/<version>/``. It passes the conformance
suite (tests/test_conformance.py). To adapt it to your backend, replace the four storage
primitives - ``_load``, ``_save``, ``_put_artifact`` and ``_get_artifact`` - with your
backend's calls, or rewrite the methods one by one, re-running the suite after each change.

Rules the core relies on (docs/adapters/registry.md in oran-adapt explains each):

* A missing model, version or alias raises ModelNotFoundError; a failing backend raises
  RegistryUnavailableError. Never let an SDK exception escape.
* Versions are "1", "2", ... per model, in creation order.
* The instance must pickle: keep clients out of ``__init__`` state or rebuild them lazily.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from oran_adapt.core.errors import ModelNotFoundError, RegistryUnavailableError
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.ports.registry import READY, ModelVersion, RegisteredModel

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings


class {{ cookiecutter.class_name }}Settings(BaseSettings):
    """This adapter's keys, read from {{ cookiecutter.env_prefix }}* environment variables.
    A key without a default is required: building the adapter fails naming it."""

    model_config = SettingsConfigDict(env_prefix="{{ cookiecutter.env_prefix }}", extra="ignore")

    root: str


class {{ cookiecutter.class_name }}:
    def __init__(self, root: str, *, artifact_policy: ArtifactPolicy) -> None:
        self.root = os.path.abspath(root)
        self.artifact_policy = artifact_policy

    @classmethod
    def from_settings(cls, settings: Settings) -> {{ cookiecutter.class_name }}:
        from pydantic import ValidationError

        from oran_adapt.core.errors import ConfigurationError

        try:
            own = {{ cookiecutter.class_name }}Settings()
        except ValidationError as exc:
            keys = ", ".join(
                "{{ cookiecutter.env_prefix }}" + str(err["loc"][0]).upper() for err in exc.errors()
            )
            raise ConfigurationError(
                f"REGISTRY_BACKEND={{ cookiecutter.adapter_name }} requires {keys}", key=keys
            ) from exc
        return cls(own.root, artifact_policy=ArtifactPolicy.from_settings(settings))

    # ---- storage primitives: replace these with your backend ---------------------------

    def _index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def _load(self) -> dict[str, Any]:
        try:
            with open(self._index_path(), encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
        except FileNotFoundError:
            return {"models": {}}
        except (OSError, ValueError) as exc:
            raise RegistryUnavailableError(f"cannot read the registry index: {exc}") from exc
        return data

    def _save(self, data: dict[str, Any]) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, self._index_path())
        except OSError as exc:
            raise RegistryUnavailableError(f"cannot write the registry index: {exc}") from exc

    def _put_artifact(self, name: str, version: str, artifact_dir: str) -> None:
        try:
            shutil.copytree(artifact_dir, os.path.join(self.root, "artifacts", name, version))
        except OSError as exc:
            raise RegistryUnavailableError(f"cannot store {name} v{version}: {exc}") from exc

    def _get_artifact(self, name: str, version: str, dst_path: str) -> str:
        local = os.path.join(dst_path, f"{name}-v{version}")
        try:
            shutil.copytree(os.path.join(self.root, "artifacts", name, version), local)
        except OSError as exc:
            raise RegistryUnavailableError(f"cannot fetch {name} v{version}: {exc}") from exc
        return local

    # ---- lookups -----------------------------------------------------------------------

    @staticmethod
    def _model(data: dict[str, Any], name: str) -> dict[str, Any]:
        model: dict[str, Any] | None = data["models"].get(check_model_name(name))
        if model is None:
            raise ModelNotFoundError(f"model {name!r} is not registered", name=name)
        return model

    def _version(self, data: dict[str, Any], name: str, version: str) -> dict[str, Any]:
        entry: dict[str, Any] | None = self._model(data, name)["versions"].get(version)
        if entry is None:
            raise ModelNotFoundError(
                f"model {name!r} has no version {version}", name=name, version=version
            )
        return entry

    @staticmethod
    def _as_version(name: str, version: str, entry: dict[str, Any]) -> ModelVersion:
        return ModelVersion(
            name=name,
            version=version,
            tags=dict(entry["tags"]),
            status=READY,
            created_at_ms=entry["created_at_ms"],
        )

    # ---- ModelRegistryPort -------------------------------------------------------------

    def ping(self) -> None:
        self._load()

    def get_registered_model(self, name: str) -> RegisteredModel:
        return RegisteredModel(name=name, aliases=dict(self._model(self._load(), name)["aliases"]))

    def list_versions(self, name: str) -> list[ModelVersion]:
        versions = self._model(self._load(), name)["versions"]
        return [self._as_version(name, v, versions[v]) for v in sorted(versions, key=int)]

    def get_version(self, name: str, version: str) -> ModelVersion:
        return self._as_version(name, version, self._version(self._load(), name, version))

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        return dict(self._version(self._load(), name, version)["metrics"])

    def get_version_by_alias(self, name: str, alias: str) -> str:
        aliases = self._model(self._load(), name)["aliases"]
        if alias not in aliases:
            raise ModelNotFoundError(f"model {name!r} has no alias {alias!r}", name=name)
        return str(aliases[alias])

    def set_alias(self, name: str, alias: str, version: str) -> None:
        data = self._load()
        self._version(data, name, version)
        self._model(data, name)["aliases"][alias] = version
        self._save(data)

    def delete_alias(self, name: str, alias: str) -> None:
        data = self._load()
        if self._model(data, name)["aliases"].pop(alias, None) is not None:
            self._save(data)

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        data = self._load()
        self._version(data, name, version)["tags"].update(tags)
        self._save(data)

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        self._version(self._load(), name, version)
        return self._get_artifact(name, version, dst_path)

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
        data = self._load()
        model = data["models"].setdefault(
            check_model_name(name), {"aliases": {}, "versions": {}}
        )
        version = str(len(model["versions"]) + 1)
        self._put_artifact(name, version, artifact_dir)
        model["versions"][version] = {
            "tags": dict(tags or {}),
            "metrics": dict(metrics or {}),
            "created_at_ms": int(time.time() * 1000),
            # Lineage: the training data's identity and schema, never its rows.
            "input": {
                "name": input_name,
                "digest": input_digest,
                "columns": None if input_frame is None else [str(c) for c in input_frame.columns],
            },
        }
        self._save(data)
        return version
