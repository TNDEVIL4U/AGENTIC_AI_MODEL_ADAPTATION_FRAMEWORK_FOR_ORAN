"""Registry adapter ``vertex``: the Vertex AI Model Registry, over its REST API.

Mapping:

- model name -> Vertex model id (lower-cased, ``.`` -> ``-``, prefixed ``m`` when it starts
  with a digit, shortened with a hash suffix past 63 characters) with ``displayName`` = the
  original name. A model whose display name differs is someone else's: ModelNotFoundError on
  read, ConflictError on create.
- version -> Vertex model version (``models/<id>@<versionId>``, numbered 1, 2, ...).
- aliases -> native version aliases (``:mergeVersionAliases``). Vertex reserves ``default``,
  which is neither reported nor settable here; Vertex alias syntax is stricter than the
  framework's (lower-case first letter, letters, digits and ``-``).
- tags, metrics, lineage -> JSON files beside the artifact in GCS
  (``gs://VERTEX_GCS_BUCKET/VERTEX_GCS_PREFIX/<id>/<uuid>/oran/``), because Vertex labels
  cannot hold checksums or free text. Tag writes are generation-matched, so concurrent
  writers never lose each other's tags.
- artifacts -> ``.../<uuid>/model/``, the version's ``artifactUri``.

Credentials are Google Application Default Credentials. The HTTP client comes from a factory
so it can be rebuilt in a job worker process; tests pass one wired to an emulator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from oran_adapt.core.errors import (
    ConflictError,
    ModelNotFoundError,
    RegistryUnavailableError,
    UnsupportedAdaptationError,
)
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.ports import AdapterSpec, Capability
from oran_adapt.ports.registry import READY, ModelVersion, RegisteredModel

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings

RESERVED_ALIAS = "default"
_ALIAS_RE = re.compile(r"^[a-z][a-zA-Z0-9-]{0,126}[a-z0-9]$")
_ID_MAX = 63
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def model_id(name: str) -> str:
    """The Vertex model id holding model ``name``."""
    base = check_model_name(name).lower().replace(".", "-")
    if not base[0].isalpha():
        base = f"m{base}"
    if base == name and len(base) <= _ID_MAX:
        return base
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return f"{base[: _ID_MAX - len(digest) - 1]}-{digest}"


class _Missing(Exception):
    """The addressed Vertex or GCS resource does not exist (HTTP 404)."""


class _Conflict(Exception):
    """HTTP 409 (already exists) or 412 (generation precondition failed)."""


class AdcToken:
    """Bearer tokens from Google Application Default Credentials, refreshed when expired."""

    def __init__(self) -> None:
        self._credentials: Any = None

    def __getstate__(self) -> dict[str, Any]:
        return {}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._credentials = None

    def __call__(self) -> str:
        import google.auth
        import google.auth.transport.requests

        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=list(_SCOPES))
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        return str(self._credentials.token)


class VertexRegistry:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        bucket: str,
        prefix: str,
        serving_image: str,
        api_endpoint: str,
        storage_endpoint: str,
        operation_timeout_s: float,
        operation_poll_s: float,
        tag_update_attempts: int,
        artifact_policy: ArtifactPolicy,
        http_factory: Callable[[], httpx.Client],
        token_provider: Callable[[], str],
    ) -> None:
        self.project = project
        self.location = location
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.serving_image = serving_image
        self.api = f"{api_endpoint.rstrip('/')}/v1"
        self.storage = storage_endpoint.rstrip("/")
        self.operation_timeout_s = operation_timeout_s
        self.operation_poll_s = operation_poll_s
        self.tag_update_attempts = tag_update_attempts
        self.artifact_policy = artifact_policy
        self.http_factory = http_factory
        self.token_provider = token_provider
        self._client: httpx.Client | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> VertexRegistry:
        location = str(settings.vertex_location)
        return cls(
            project=str(settings.vertex_project),
            location=location,
            bucket=str(settings.vertex_gcs_bucket),
            prefix=settings.vertex_gcs_prefix,
            serving_image=str(settings.vertex_serving_image),
            api_endpoint=settings.vertex_api_endpoint
            or f"https://{location}-aiplatform.googleapis.com",
            storage_endpoint=settings.vertex_storage_endpoint,
            operation_timeout_s=settings.vertex_operation_timeout_s,
            operation_poll_s=settings.vertex_operation_poll_s,
            tag_update_attempts=settings.vertex_tag_update_attempts,
            artifact_policy=ArtifactPolicy.from_settings(settings),
            http_factory=partial(httpx.Client, timeout=settings.vertex_http_timeout_s),
            token_provider=AdcToken(),
        )

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_client"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)

    # ---- HTTP --------------------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.http_factory()
        return self._client

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token_provider()}", **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RegistryUnavailableError(
                "Vertex AI or GCS is not reachable", url=url, cause=str(exc)
            ) from exc
        if response.status_code == 404:
            raise _Missing(url)
        if response.status_code in (409, 412):
            raise _Conflict(response.text)
        if response.status_code >= 400:
            raise RegistryUnavailableError(
                f"Vertex AI request failed with HTTP {response.status_code}",
                url=url,
                cause=response.text[:500],
            )
        return response

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        body: dict[str, Any] = self._request(method, url, **kwargs).json()
        return body

    @property
    def _parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}"

    def _model_path(self, name: str) -> str:
        return f"{self._parent}/models/{model_id(name)}"

    # ---- GCS ---------------------------------------------------------------------------

    def _object_url(self, obj: str) -> str:
        return f"{self.storage}/storage/v1/b/{self.bucket}/o/{quote(obj, safe='')}"

    def _gcs_put(self, obj: str, data: bytes, generation: str | None = None) -> None:
        params = {"uploadType": "media", "name": obj}
        if generation is not None:
            params["ifGenerationMatch"] = generation
        self._request(
            "POST",
            f"{self.storage}/upload/storage/v1/b/{self.bucket}/o",
            params=params,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    def _gcs_get(self, obj: str) -> tuple[bytes, str]:
        response = self._request("GET", self._object_url(obj), params={"alt": "media"})
        return response.content, response.headers.get("x-goog-generation", "0")

    def _gcs_list(self, prefix: str) -> list[str]:
        names: list[str] = []
        token: str | None = None
        while True:
            params = {"prefix": prefix}
            if token:
                params["pageToken"] = token
            page = self._json(
                "GET", f"{self.storage}/storage/v1/b/{self.bucket}/o", params=params
            )
            names.extend(item["name"] for item in page.get("items", []))
            token = page.get("nextPageToken")
            if not token:
                return names

    def _base(self, model: dict[str, Any]) -> str:
        """The GCS object prefix of a version (without ``gs://<bucket>/``)."""
        uri = str(model.get("artifactUri", ""))
        head = f"gs://{self.bucket}/"
        if not uri.startswith(head) or not uri.rstrip("/").endswith("/model"):
            raise RegistryUnavailableError(
                "the Vertex model version was not created by this adapter", artifact_uri=uri
            )
        return uri[len(head) :].rstrip("/")[: -len("/model")]

    def _sidecar(self, model: dict[str, Any], file: str) -> tuple[dict[str, Any], str | None]:
        try:
            data, generation = self._gcs_get(f"{self._base(model)}/oran/{file}")
        except _Missing:
            return {}, None
        return json.loads(data), generation

    # ---- models and versions -----------------------------------------------------------

    def _model(self, name: str) -> dict[str, Any]:
        try:
            model = self._json("GET", f"{self.api}/{self._model_path(name)}")
        except _Missing as exc:
            raise ModelNotFoundError(f"Model '{name}' not found", model=name) from exc
        if model.get("displayName") != name:
            raise ModelNotFoundError(
                f"Model '{name}' not found; Vertex model {model_id(name)!r} is "
                f"{model.get('displayName')!r}",
                model=name,
            )
        return model

    def _raw_version(self, name: str, version: str) -> dict[str, Any]:
        self._model(name)
        if not version.isdigit():
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            )
        try:
            return self._json("GET", f"{self.api}/{self._model_path(name)}@{version}")
        except _Missing as exc:
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            ) from exc

    def _version(self, name: str, model: dict[str, Any]) -> ModelVersion:
        tags, _ = self._sidecar(model, "tags.json")
        created = model.get("versionCreateTime") or model.get("createTime")
        return ModelVersion(
            name=name,
            version=str(model["versionId"]),
            tags=tags,
            status=READY,
            created_at_ms=int(datetime.fromisoformat(created).timestamp() * 1000)
            if created
            else None,
            source=f"{self._model_path(name)}@{model['versionId']}",
        )

    @staticmethod
    def _check_alias(alias: str) -> None:
        if alias == RESERVED_ALIAS or not _ALIAS_RE.match(alias):
            raise UnsupportedAdaptationError(
                f"alias {alias!r} cannot be a Vertex version alias",
                alias=alias,
                pattern=_ALIAS_RE.pattern,
                reserved=RESERVED_ALIAS,
            )

    def _merge_aliases(self, name: str, version: str, aliases: list[str]) -> None:
        self._request(
            "POST",
            f"{self.api}/{self._model_path(name)}@{version}:mergeVersionAliases",
            json={"versionAliases": aliases},
        )

    def _wait(self, operation: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.operation_timeout_s
        while not operation.get("done"):
            if time.monotonic() >= deadline:
                raise RegistryUnavailableError(
                    "timed out waiting for the Vertex model upload",
                    operation=operation.get("name"),
                    timeout_s=self.operation_timeout_s,
                )
            time.sleep(self.operation_poll_s)
            operation = self._json("GET", f"{self.api}/{operation['name']}")
        if "error" in operation:
            raise RegistryUnavailableError(
                "the Vertex model upload failed", cause=json.dumps(operation["error"])[:500]
            )
        response: dict[str, Any] = operation.get("response", {})
        return response

    # ---- port --------------------------------------------------------------------------

    def ping(self) -> None:
        try:
            self._request("GET", f"{self.api}/{self._parent}/models", params={"pageSize": 1})
            self._request("GET", f"{self.storage}/storage/v1/b/{self.bucket}")
        except _Missing as exc:
            raise RegistryUnavailableError(
                "the Vertex location or GCS bucket does not exist",
                project=self.project,
                location=self.location,
                bucket=self.bucket,
            ) from exc

    def get_registered_model(self, name: str) -> RegisteredModel:
        aliases: dict[str, str] = {}
        for version in self._list_raw(name):
            for alias in version.get("versionAliases", []):
                if alias != RESERVED_ALIAS:
                    aliases[alias] = str(version["versionId"])
        return RegisteredModel(name=name, aliases=aliases)

    def _list_raw(self, name: str) -> list[dict[str, Any]]:
        self._model(name)
        models: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {"pageToken": token} if token else {}
            page = self._json(
                "GET", f"{self.api}/{self._model_path(name)}:listVersions", params=params
            )
            models.extend(page.get("models", []))
            token = page.get("nextPageToken")
            if not token:
                return sorted(models, key=lambda m: int(m["versionId"]))

    def list_versions(self, name: str) -> list[ModelVersion]:
        return [self._version(name, m) for m in self._list_raw(name)]

    def get_version(self, name: str, version: str) -> ModelVersion:
        return self._version(name, self._raw_version(name, version))

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        metrics, _ = self._sidecar(self._raw_version(name, version), "metrics.json")
        return {k: float(v) for k, v in metrics.items()}

    def get_version_by_alias(self, name: str, alias: str) -> str:
        version = self.get_registered_model(name).aliases.get(alias)
        if version is None:
            raise ModelNotFoundError(
                f"model '{name}' has no version aliased '{alias}'", model=name, alias=alias
            )
        return version

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._check_alias(alias)
        self._raw_version(name, version)
        current = self.get_registered_model(name).aliases.get(alias)
        if current == version:
            return
        if current is not None:
            self._merge_aliases(name, current, [f"-{alias}"])
        self._merge_aliases(name, version, [alias])

    def delete_alias(self, name: str, alias: str) -> None:
        current = self.get_registered_model(name).aliases.get(alias)
        if current is not None:
            self._merge_aliases(name, current, [f"-{alias}"])

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        model = self._raw_version(name, version)
        obj = f"{self._base(model)}/oran/tags.json"
        for _ in range(self.tag_update_attempts):
            current, generation = self._sidecar(model, "tags.json")
            merged = {**current, **{k: str(v) for k, v in tags.items()}}
            try:
                self._gcs_put(obj, json.dumps(merged, sort_keys=True).encode(), generation or "0")
                return
            except _Conflict:
                continue
        raise RegistryUnavailableError(
            "the version's tags kept changing under this write",
            model=name,
            version=version,
            attempts=self.tag_update_attempts,
        )

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        prefix = f"{self._base(self._raw_version(name, version))}/model/"
        target = os.path.join(dst_path, "model")
        os.makedirs(target, exist_ok=True)
        for obj in self._gcs_list(prefix):
            rel = obj[len(prefix) :]
            parts = rel.split("/")
            if not rel or any(p in ("", ".", "..") for p in parts):
                raise RegistryUnavailableError("unsafe object name in the artifact", object=obj)
            local = os.path.join(target, *parts)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            data, _ = self._gcs_get(obj)
            with open(local, "wb") as fh:
                fh.write(data)
        return target

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
        mid = model_id(name)
        try:
            self._model(name)
            exists = True
        except ModelNotFoundError:
            try:
                self._json("GET", f"{self.api}/{self._model_path(name)}")
            except _Missing:
                exists = False
            else:
                raise ConflictError(
                    f"Vertex model id {mid!r} already holds another model",
                    model=name,
                    model_id=mid,
                ) from None
        base = f"{self.prefix}/{mid}/{uuid.uuid4().hex}"
        for root, _, files in os.walk(artifact_dir):
            for file in sorted(files):
                path = os.path.join(root, file)
                rel = os.path.relpath(path, artifact_dir).replace(os.sep, "/")
                with open(path, "rb") as fh:
                    self._gcs_put(f"{base}/model/{rel}", fh.read())
        sidecars: dict[str, Any] = {
            "tags.json": {k: str(v) for k, v in (tags or {}).items()},
            "metrics.json": {k: float(v) for k, v in (metrics or {}).items()},
        }
        if input_frame is not None:
            sidecars["lineage.json"] = {
                "name": input_name,
                "digest": input_digest,
                "schema": {str(c): str(t) for c, t in input_frame.dtypes.items()},
                "rows": len(input_frame),
            }
        for file, data in sidecars.items():
            self._gcs_put(f"{base}/oran/{file}", json.dumps(data, sort_keys=True).encode())
        body: dict[str, Any] = {
            "model": {
                "displayName": name,
                "artifactUri": f"gs://{self.bucket}/{base}/model/",
                "containerSpec": {"imageUri": self.serving_image},
            }
        }
        if exists:
            body["parentModel"] = self._model_path(name)
        else:
            body["modelId"] = mid
        try:
            operation = self._json("POST", f"{self.api}/{self._parent}/models:upload", json=body)
        except _Conflict:
            # Another writer created the model first; this upload becomes its next version.
            body.pop("modelId", None)
            body["parentModel"] = self._model_path(name)
            operation = self._json("POST", f"{self.api}/{self._parent}/models:upload", json=body)
        return str(self._wait(operation)["modelVersionId"])


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="vertex",
        description="Vertex AI Model Registry over REST; artifacts, tags and metrics in GCS",
        features=frozenset({"aliases", "version_tags", "version_metrics", "lineage_inputs"}),
        config_keys=(
            "vertex_project",
            "vertex_location",
            "vertex_gcs_bucket",
            "vertex_gcs_prefix",
            "vertex_serving_image",
            "vertex_api_endpoint",
            "vertex_storage_endpoint",
            "vertex_http_timeout_s",
            "vertex_operation_timeout_s",
            "vertex_operation_poll_s",
            "vertex_tag_update_attempts",
            "registry_tags_checksum",
            "registry_tags_status",
            "artifact_max_bytes",
            "artifact_hash_chunk_bytes",
        ),
        required_keys=(
            "vertex_project",
            "vertex_location",
            "vertex_gcs_bucket",
            "vertex_serving_image",
        ),
        distributions=("google-auth",),
    ),
    factory=VertexRegistry.from_settings,
)
