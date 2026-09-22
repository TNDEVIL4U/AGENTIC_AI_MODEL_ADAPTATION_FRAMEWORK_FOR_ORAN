"""Thin wrapper over MLflow — the single authority for models, versions, artifacts, aliases."""

from __future__ import annotations

import os
from typing import Any

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from oran_adapt.core.errors import ModelNotFoundError, RegistryUnavailableError


class MlflowRegistry:
    def __init__(self, tracking_uri: str, registry_uri: str | None = None) -> None:
        # MLflow's default HTTP policy (7 retries with exponential backoff) makes an outage
        # take minutes to surface. Fail fast unless the operator configured otherwise.
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "0")
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
        self.tracking_uri = tracking_uri
        self.registry_uri = registry_uri or tracking_uri
        self.client = MlflowClient(tracking_uri=tracking_uri, registry_uri=self.registry_uri)

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

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        """Download a registered model version's artifacts to a local directory and return the
        local path. Member 3's loaders load the native model object from that path."""
        import mlflow.artifacts

        try:
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

    def register_candidate(self, name: str, artifact_path: str, *, framework: str, metrics: dict[str, float]) -> str:
        """Log a candidate artifact (a local joblib file, as produced by Member 3's engines) to
        MLflow as a new run and register it as a new version of ``name``. Returns the new
        version number. Raises UnsupportedAdaptationError if ``framework`` has no MLflow log_model
        flavor wired up."""
        import joblib
        import mlflow

        from oran_adapt.core.errors import UnsupportedAdaptationError

        fw = framework.lower()
        model = joblib.load(artifact_path)

        # mlflow.*.log_model only knows how to talk to the *global* fluent tracking/registry URI,
        # so we point it at this registry's backend for the duration of the call - and always
        # restore whatever was there before, so this call never leaves lasting global side effects
        # for any other MlflowRegistry instance (or test) running in the same process.
        prev_tracking_uri = mlflow.get_tracking_uri()
        prev_registry_uri = mlflow.get_registry_uri()
        mlflow.set_tracking_uri(self.tracking_uri)
        mlflow.set_registry_uri(self.registry_uri)
        try:
            with mlflow.start_run():
                if fw == "sklearn":
                    info = mlflow.sklearn.log_model(model, name="model", registered_model_name=name)
                elif fw == "xgboost":
                    info = mlflow.xgboost.log_model(model, name="model", registered_model_name=name)
                elif fw in ("torch", "pytorch"):
                    info = mlflow.pytorch.log_model(
                        model, name="model", registered_model_name=name, serialization_format="pickle"
                    )
                else:
                    raise UnsupportedAdaptationError(
                        f"no MLflow log_model flavor for framework {framework!r}"
                    )
                for metric_name, value in metrics.items():
                    mlflow.log_metric(metric_name, value)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow model registration failed", cause=str(exc)) from exc
        finally:
            mlflow.set_tracking_uri(prev_tracking_uri)
            mlflow.set_registry_uri(prev_registry_uri)
        return str(info.registered_model_version)
