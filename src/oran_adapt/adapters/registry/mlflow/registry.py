"""The ``mlflow`` registry adapter's implementation; the only module besides ``flavors``
that imports the MLflow SDK. ``__init__`` holds its spec, importable without MLflow."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from oran_adapt.core.errors import ModelNotFoundError, RegistryUnavailableError
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.ports import ModelVersion, RegisteredModel
from oran_adapt.ports.registry import FAILED, PENDING, READY

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings

_NOT_FOUND = "RESOURCE_DOES_NOT_EXIST"
_INVALID = "INVALID_PARAMETER_VALUE"
# Where a version's artifact directory sits inside its run.
_ARTIFACT_PATH = "model"
_STATUS = {"READY": READY, "PENDING_REGISTRATION": PENDING, "FAILED_REGISTRATION": FAILED}


def _code(exc: MlflowException) -> str:
    return str(getattr(exc, "error_code", ""))


class MlflowRegistry:
    def __init__(
        self,
        tracking_uri: str,
        registry_uri: str | None = None,
        *,
        artifact_policy: ArtifactPolicy,
        http_max_retries: int,
        http_backoff_factor: float,
        http_timeout_s: float,
        telemetry: bool = False,
    ) -> None:
        # MLflow's default HTTP policy (7 retries with exponential backoff) makes an outage take
        # minutes to surface. Fail fast unless the operator set MLflow's own variables.
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", str(http_max_retries))
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", str(http_backoff_factor))
        os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", str(int(http_timeout_s)))
        if not telemetry:
            # MLflow reports usage to its vendor's servers unless told not to; the framework
            # makes no call the operator did not configure (MLFLOW_TELEMETRY opts in).
            os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
            from mlflow.telemetry import set_telemetry_client

            set_telemetry_client()  # drops the client MLflow started on import
        self.tracking_uri = tracking_uri
        self.registry_uri = registry_uri or tracking_uri
        self.artifact_policy = artifact_policy
        self._http = (http_max_retries, http_backoff_factor, http_timeout_s)
        self._telemetry = telemetry
        self.client = MlflowClient(tracking_uri=tracking_uri, registry_uri=self.registry_uri)

    @classmethod
    def from_settings(cls, settings: Settings) -> MlflowRegistry:
        return cls(
            settings.mlflow_tracking_uri,
            settings.mlflow_registry_uri,
            artifact_policy=ArtifactPolicy.from_settings(settings),
            http_max_retries=settings.mlflow_http_max_retries,
            http_backoff_factor=settings.mlflow_http_backoff_factor,
            http_timeout_s=settings.mlflow_http_timeout_s,
            telemetry=settings.mlflow_telemetry,
        )

    # The MlflowClient holds sessions; a job worker process rebuilds it from the URIs.
    def __getstate__(self) -> dict[str, Any]:
        return {
            "tracking_uri": self.tracking_uri,
            "registry_uri": self.registry_uri,
            "artifact_policy": self.artifact_policy,
            "http": self._http,
            "telemetry": self._telemetry,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        retries, backoff, timeout = state["http"]
        self.__init__(  # type: ignore[misc]
            state["tracking_uri"],
            state["registry_uri"],
            artifact_policy=state["artifact_policy"],
            http_max_retries=retries,
            http_backoff_factor=backoff,
            http_timeout_s=timeout,
            telemetry=state.get("telemetry", False),
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

    def get_registered_model(self, name: str) -> RegisteredModel:
        try:
            model = self.client.get_registered_model(name)
        except MlflowException as exc:
            if _code(exc) == _NOT_FOUND:
                raise ModelNotFoundError(f"Model '{name}' not found in MLflow", model=name) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc
        aliases = {alias: str(version) for alias, version in (model.aliases or {}).items()}
        return RegisteredModel(name=name, aliases=aliases)

    @staticmethod
    def _version(mv: Any) -> ModelVersion:
        return ModelVersion(
            name=mv.name,
            version=str(mv.version),
            tags=dict(mv.tags or {}),
            status=_STATUS.get(str(mv.status), READY if mv.status is None else PENDING),
            created_at_ms=mv.creation_timestamp,
            source=mv.run_id,
        )

    def list_versions(self, name: str) -> list[ModelVersion]:
        self.get_registered_model(name)
        try:
            versions = self.client.search_model_versions(f"name='{name}'")
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc
        return sorted((self._version(v) for v in versions), key=lambda v: int(v.version))

    def _raw_version(self, name: str, version: str) -> Any:
        try:
            return self.client.get_model_version(name, version)
        except MlflowException as exc:
            if _code(exc) in (_NOT_FOUND, _INVALID):
                raise ModelNotFoundError(
                    f"Model '{name}' version '{version}' not found", model=name, version=version
                ) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc

    def get_version(self, name: str, version: str) -> ModelVersion:
        """One registered version. Raises ModelNotFoundError if it does not exist."""
        return self._version(self._raw_version(name, version))

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        """The metrics logged on the version's source run; empty when the version has no run
        or the run has been deleted."""
        run_id = self._raw_version(name, version).run_id
        if not run_id:
            return {}
        try:
            return dict(self.client.get_run(run_id).data.metrics or {})
        except MlflowException as exc:
            if _code(exc) == _NOT_FOUND:
                return {}
            raise RegistryUnavailableError("MLflow run lookup failed", cause=str(exc)) from exc

    @contextmanager
    def _fluent_uris(self) -> Iterator[None]:
        """Point MLflow's *global* tracking/registry URIs at this registry for the duration of
        the block, then restore whatever was there before. Needed because the fluent APIs
        (``mlflow.start_run``, ``mlflow.log_artifacts``) and the resolution of a model version's
        nested ``models:/m-<id>`` source only consult the global URIs, whatever is passed
        explicitly - without this they silently talk to a different store.

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
        import mlflow.artifacts

        self._raw_version(name, version)  # ModelNotFoundError before any download
        os.makedirs(dst_path, exist_ok=True)
        try:
            with self._fluent_uris():
                return mlflow.artifacts.download_artifacts(
                    artifact_uri=f"models:/{name}/{version}",
                    dst_path=dst_path,
                    tracking_uri=self.tracking_uri,
                    registry_uri=self.registry_uri,
                )
        except MlflowException as exc:
            if _code(exc) == _NOT_FOUND:
                raise ModelNotFoundError(
                    f"Model '{name}' version '{version}' not found", model=name, version=version
                ) from exc
            raise RegistryUnavailableError(
                "MLflow artifact download failed", cause=str(exc)
            ) from exc

    def get_version_by_alias(self, name: str, alias: str) -> str:
        """The version currently under ``alias``. Raises ModelNotFoundError if the model has no
        version under that alias yet."""
        try:
            return str(self.client.get_model_version_by_alias(name, alias).version)
        except MlflowException as exc:
            if _code(exc) in (_NOT_FOUND, _INVALID):
                raise ModelNotFoundError(
                    f"model '{name}' has no version aliased '{alias}'", model=name, alias=alias
                ) from exc
            raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._raw_version(name, version)
        try:
            self.client.set_registered_model_alias(name, alias, version)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow alias update failed", cause=str(exc)) from exc

    def delete_alias(self, name: str, alias: str) -> None:
        """Remove ``alias``; a no-op when it is not set. ModelNotFoundError if the model is
        absent."""
        if alias not in self.get_registered_model(name).aliases:
            return
        try:
            self.client.delete_registered_model_alias(name, alias)
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow alias delete failed", cause=str(exc)) from exc

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        self._raw_version(name, version)
        try:
            for key, value in tags.items():
                self.client.set_model_version_tag(name, version, key, str(value))
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow tag update failed", cause=str(exc)) from exc

    def _ensure_registered_model(self, name: str) -> None:
        try:
            self.client.get_registered_model(name)
        except MlflowException as exc:
            if _code(exc) != _NOT_FOUND:
                raise RegistryUnavailableError("MLflow query failed", cause=str(exc)) from exc
            self.client.create_registered_model(name)

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
        """Log ``artifact_dir`` as a new run and register it as the next version of ``name``.
        ``tags`` are set on both the run and the model version (that is where data lineage
        lives - see datastore.versioning); ``input_frame`` becomes the run's training dataset
        (schema and digest only, not the rows)."""
        import mlflow
        from mlflow.data.pandas_dataset import from_pandas

        try:
            with self._fluent_uris(), mlflow.start_run() as run:
                if input_frame is not None:
                    dataset = from_pandas(input_frame, name=input_name, digest=input_digest)
                    mlflow.log_input(dataset, context="training")
                mlflow.log_artifacts(artifact_dir, artifact_path=_ARTIFACT_PATH)
                for metric_name, value in (metrics or {}).items():
                    mlflow.log_metric(metric_name, value)
                if tags:
                    mlflow.set_tags(tags)
            run_id = run.info.run_id
            self._ensure_registered_model(name)
            mv = self.client.create_model_version(
                name,
                source=f"runs:/{run_id}/{_ARTIFACT_PATH}",
                run_id=run_id,
                tags={k: str(v) for k, v in (tags or {}).items()},
            )
        except MlflowException as exc:
            raise RegistryUnavailableError("MLflow model registration failed", cause=str(exc)) from exc
        return str(mv.version)
