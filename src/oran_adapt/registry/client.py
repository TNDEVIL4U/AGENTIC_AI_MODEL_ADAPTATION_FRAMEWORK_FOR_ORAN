"""Thin wrapper over MLflow — the single authority for models, versions, artifacts, aliases."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pandas as pd
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from oran_adapt.core.errors import (
    ArtifactError,
    ModelNotFoundError,
    RegistryUnavailableError,
    UnsupportedAdaptationError,
)

# Types beyond skops' built-in safe list that sklearn tree models need (see
# Settings.mlflow_skops_trusted_types, which overrides this default).
DEFAULT_SKOPS_TRUSTED_TYPES = (
    "sklearn.tree._tree.Tree",
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
)


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


class MlflowRegistry:
    def __init__(
        self,
        tracking_uri: str,
        registry_uri: str | None = None,
        *,
        skops_trusted_types: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        # MLflow's default HTTP policy (7 retries with exponential backoff) makes an outage
        # take minutes to surface. Fail fast unless the operator configured otherwise.
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "0")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
        self.tracking_uri = tracking_uri
        self.registry_uri = registry_uri or tracking_uri
        self.client = MlflowClient(tracking_uri=tracking_uri, registry_uri=self.registry_uri)
        self.skops_trusted_types = tuple(
            DEFAULT_SKOPS_TRUSTED_TYPES if skops_trusted_types is None else skops_trusted_types
        )

    def ping(self) -> None:
        """Raise RegistryUnavailableError if the tracking/registry backend is unreachable."""
        try:
            self.client.search_registered_models(max_results=1)
            self.client.search_experiments(max_results=1)
        except Exception as exc:
            raise RegistryUnavailableError(
                "MLflow is not reachable", uri=self.tracking_uri, cause=str(exc)
            ) from exc

    def get_registered_model(self, name: str) -> Any:
        try:
            return self.client.get_registered_model(name)
        except MlflowException as exc:
            if getattr(exc, "error_code", "") == "RESOURCE_DOES_NOT_EXIST":
                raise ModelNotFoundError(f"Model '{name}' not found in MLflow", model=name) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc

    def list_versions(self, name: str) -> list[Any]:
        self.get_registered_model(name)
        try:
            versions = self.client.search_model_versions(f"name='{name}'")
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc
        return sorted(versions, key=lambda v: int(v.version))

    @contextmanager
    def _fluent_uris(self) -> Iterator[None]:
        """Point MLflow's *global* tracking/registry URIs at this registry for the duration of
        the block, then restore whatever was there before. Needed because the fluent APIs
        (``mlflow.<flavor>.log_model``) and the resolution of a model version's nested
        ``models:/m-<id>`` source only consult the global URIs, whatever is passed explicitly -
        without this they silently talk to a different store.

        The setters also write MLFLOW_TRACKING_URI / MLFLOW_REGISTRY_URI into os.environ, which
        Settings reads, so the previous *unset* state is restored exactly (not replaced by
        MLflow's resolved default) - otherwise every later Settings() in the process would
        silently pick up a registry URI nobody configured."""
        import mlflow
        from mlflow.tracking import _model_registry, _tracking_service

        env_keys = ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI")
        saved_env = {key: os.environ.get(key) for key in env_keys}
        tracking_mod = _tracking_service.utils
        registry_mod = _model_registry.utils
        saved_tracking = getattr(tracking_mod, "_tracking_uri", None)
        saved_registry = getattr(registry_mod, "_registry_uri", None)
        mlflow.set_tracking_uri(self.tracking_uri)
        mlflow.set_registry_uri(self.registry_uri)
        try:
            yield
        finally:
            if hasattr(tracking_mod, "_tracking_uri"):
                tracking_mod._tracking_uri = saved_tracking
            if hasattr(registry_mod, "_registry_uri"):
                registry_mod._registry_uri = saved_registry
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        """Download a registered model version's artifacts to a local directory and return the
        local path. Member 3's loaders load the native model object from that path."""
        import mlflow.artifacts

        try:
            with self._fluent_uris():
                return mlflow.artifacts.download_artifacts(
                    artifact_uri=f"models:/{name}/{version}",
                    dst_path=dst_path,
                    tracking_uri=self.tracking_uri,
                    registry_uri=self.registry_uri,
                )
        except MlflowException as exc:
            if getattr(exc, "error_code", "") == "RESOURCE_DOES_NOT_EXIST":
                raise ModelNotFoundError(
                    f"Model '{name}' version '{version}' not found", model=name, version=version
                ) from exc
            raise RegistryUnavailableError(
                "MLflow artifact download failed", cause=str(exc)
            ) from exc

    def get_version_by_alias(self, name: str, alias: str) -> str:
        """The version currently serving traffic under ``alias`` (e.g. the orchestrator's
        ``live_alias``). Raises ModelNotFoundError if the model has no version under that
        alias yet."""
        try:
            return str(self.client.get_model_version_by_alias(name, alias).version)
        except MlflowException as exc:
            if getattr(exc, "error_code", "") == "RESOURCE_DOES_NOT_EXIST":
                raise ModelNotFoundError(
                    f"model '{name}' has no version aliased '{alias}'", model=name, alias=alias
                ) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc

    def set_alias(self, name: str, alias: str, version: str) -> None:
        try:
            self.client.set_registered_model_alias(name, alias, version)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow alias update failed", cause=str(exc)) from exc

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
    ) -> str:
        """Log a native model object to MLflow as a new run and register it as a new version of
        ``name``; returns the version number. ``tags`` are set on both the run and the model
        version (that is where data lineage lives - see datastore.versioning). ``input_frame``
        is recorded as the run's training dataset (schema + digest only, not the rows).

        Raises UnsupportedAdaptationError for a framework with no MLflow flavor, ArtifactError
        for a model that cannot be serialized safely, RegistryUnavailableError when MLflow
        itself fails."""
        import mlflow

        fw = framework.lower()
        if fw not in ("sklearn", "xgboost", "torch", "pytorch"):
            raise UnsupportedAdaptationError(f"no MLflow log_model flavor for framework {framework!r}")
        trusted = (
            resolve_skops_trusted_types(model, self.skops_trusted_types) if fw == "sklearn" else []
        )

        try:
            with self._fluent_uris(), mlflow.start_run():
                if input_frame is not None:
                    dataset = mlflow.data.from_pandas(
                        input_frame, name=input_name, digest=input_digest
                    )
                    mlflow.log_input(dataset, context="training")
                if fw == "sklearn":
                    info = mlflow.sklearn.log_model(
                        model,
                        name="model",
                        registered_model_name=name,
                        skops_trusted_types=trusted or None,
                    )
                elif fw == "xgboost":
                    info = mlflow.xgboost.log_model(model, name="model", registered_model_name=name)
                else:
                    info = mlflow.pytorch.log_model(
                        model, name="model", registered_model_name=name, serialization_format="pickle"
                    )
                for metric_name, value in (metrics or {}).items():
                    mlflow.log_metric(metric_name, value)
                if tags:
                    mlflow.set_tags(tags)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow model registration failed", cause=str(exc)) from exc

        version = str(info.registered_model_version)
        if tags:
            self.set_version_tags(name, version, tags)
        return version

    def register_candidate(
        self,
        name: str,
        artifact_path: str,
        *,
        framework: str,
        metrics: dict[str, float],
        tags: dict[str, str] | None = None,
    ) -> str:
        """Register a candidate artifact (a local joblib file, as produced by Member 3's
        engines) as a new version of ``name``. See log_model for the errors it raises."""
        import joblib

        try:
            model = joblib.load(artifact_path)
        except Exception as exc:
            raise ArtifactError(
                f"could not load candidate artifact: {artifact_path}", cause=str(exc)
            ) from exc
        return self.log_model(name, model, framework=framework, metrics=metrics, tags=tags)

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        try:
            for key, value in tags.items():
                self.client.set_model_version_tag(name, version, key, str(value))
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow tag update failed", cause=str(exc)) from exc

    def get_version(self, name: str, version: str) -> Any:
        """One registered version (tags, run id, creation time). Raises ModelNotFoundError if
        it does not exist."""
        try:
            return self.client.get_model_version(name, version)
        except MlflowException as exc:
            if getattr(exc, "error_code", "") in ("RESOURCE_DOES_NOT_EXIST", "INVALID_PARAMETER_VALUE"):
                raise ModelNotFoundError(
                    f"Model '{name}' version '{version}' not found", model=name, version=version
                ) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc

    def get_run_metrics(self, run_id: str | None) -> dict[str, float]:
        """The metrics logged on a version's source run (its training-time baseline); empty
        when the version has no run or the run is gone."""
        if not run_id:
            return {}
        try:
            return dict(self.client.get_run(run_id).data.metrics or {})
        except MlflowException:
            return {}

    def delete_alias(self, name: str, alias: str) -> None:
        try:
            self.client.delete_registered_model_alias(name, alias)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow alias delete failed", cause=str(exc)) from exc

    def record_artifact_checksum(self, name: str, version: str, workdir: str) -> str:
        """Download ``version``'s artifacts, hash them and store the hash as the
        ``artifact.sha256`` version tag; returns the hash. Called right after registration so
        every later load can be checked against it."""
        from oran_adapt.core.integrity import sha256_path

        local = self.download_artifacts(name, version, os.path.join(workdir, f"sha-{version}"))
        digest = sha256_path(local)
        self.set_version_tags(name, version, {"artifact.sha256": digest})
        return digest

    def describe_versions(self, name: str) -> list[dict[str, Any]]:
        """Every registered version of ``name`` with its aliases, tags and source run - the
        registry half of a model's lineage (the data half lives in the database)."""
        versions = self.list_versions(name)
        # search_model_versions does not return aliases; the registered model has the map.
        try:
            alias_map = dict(self.client.get_registered_model(name).aliases or {})
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow alias lookup failed", cause=str(exc)) from exc
        by_version: dict[str, list[str]] = {}
        for alias, version in alias_map.items():
            by_version.setdefault(str(version), []).append(alias)
        return [
            {
                "version": str(v.version),
                "aliases": sorted(by_version.get(str(v.version), [])),
                "tags": dict(v.tags or {}),
                "run_id": v.run_id,
                "status": v.status,
                "created_at_ms": v.creation_timestamp,
            }
            for v in versions
        ]
