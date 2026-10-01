"""Deployment adapter ``triton``: NVIDIA Triton Inference Server, or any server implementing
the KServe v2 / Open Inference Protocol model-repository extension, in explicit model-control
mode (``--model-control-mode=explicit``).

``deploy`` stages the verified artifact directory as ``TRITON_REPOSITORY/<model>/<version>/``
(the server must see the same directory, e.g. a shared volume), writes
``<model>/config.pbtxt`` as TRITON_BASE_CONFIG plus a version policy naming exactly that
version, and calls ``POST /v2/repository/models/<model>/load``, which Triton answers once the
load finished. ``status`` reads ``POST /v2/repository/index``: the version whose state is
READY. Staged versions are kept, so restoring an earlier one needs no download; a version that
was never staged here cannot be restored without its artifact.

The artifact must already be in a layout the Triton backend can load (e.g. ``model.onnx``,
``model.pt``); the framework does not convert models.
"""

from __future__ import annotations

import os
import shutil
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from oran_adapt.adapters.deployment._common import HttpApi, StaticToken, required
from oran_adapt.core.errors import ConfigurationError, DeploymentError
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class TritonDeployment:
    def __init__(self, api: HttpApi, *, repository: str, base_config: str) -> None:
        self.api = api
        self.repository = Path(repository)
        self.base_config = base_config

    @classmethod
    def from_settings(cls, settings: Settings) -> TritonDeployment:
        base = ""
        if settings.triton_base_config:
            try:
                base = Path(settings.triton_base_config).read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigurationError(
                    "TRITON_BASE_CONFIG cannot be read", key="triton_base_config", cause=str(exc)
                ) from exc
            if "version_policy" in base:
                raise ConfigurationError(
                    "TRITON_BASE_CONFIG must not set version_policy; the adapter writes it",
                    key="triton_base_config",
                )
        repository = required(settings.triton_repository, "triton_repository")
        if not Path(repository).is_dir():
            raise ConfigurationError(
                "TRITON_REPOSITORY is not a directory", key="triton_repository", path=repository
            )
        return cls(
            HttpApi(
                required(settings.triton_url, "triton_url"),
                service="the Triton server",
                http_factory=partial(
                    OutboundPolicy.from_settings(settings).client,
                    timeout=settings.deployment_http_timeout_s,
                ),
                token=StaticToken(None),
            ),
            repository=repository,
            base_config=base,
        )

    def _check(self, response: httpx.Response, action: str, model: str) -> None:
        if response.status_code >= 400:
            try:
                reason = str(response.json().get("error", response.text))
            except ValueError:
                reason = response.text
            raise DeploymentError(
                f"Triton refused to {action} model {model} (HTTP {response.status_code})",
                model=model,
                cause=reason[:500],
            )

    def _stage(self, target: DeploymentTarget) -> None:
        name = check_model_name(target.model)
        if not target.version.isdigit():
            raise DeploymentError(
                "Triton versions are positive integers", model=target.model, version=target.version
            )
        dest = self.repository / name / target.version
        if dest.is_dir():
            return  # versions are immutable: an existing directory is this version
        if target.artifact_dir is None:
            raise DeploymentError(
                f"version {target.version} is not staged in TRITON_REPOSITORY and no artifact "
                "was supplied",
                model=target.model,
                version=target.version,
            )
        staging = dest.parent / f".staging-{target.version}-{uuid.uuid4().hex[:8]}"
        shutil.copytree(target.artifact_dir, staging)
        os.replace(staging, dest)

    def _write_config(self, model: str, version: str) -> None:
        name = check_model_name(model)
        config = self.base_config.rstrip()
        if config:
            config += "\n"
        config += f"version_policy: {{ specific: {{ versions: [{int(version)}] }} }}\n"
        path = self.repository / name / "config.pbtxt"
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(config, encoding="utf-8")
        os.replace(tmp, path)

    def _load(self, target: DeploymentTarget) -> None:
        self._stage(target)
        self._write_config(target.model, target.version)
        name = check_model_name(target.model)
        response = self.api.call("POST", f"/v2/repository/models/{name}/load", json={})
        self._check(response, "load", target.model)

    def ping(self) -> None:
        response = self.api.call("GET", "/v2/health/live")
        self._check(response, "answer the liveness probe for", "_server")

    def status(self, model: str) -> DeploymentState:
        name = check_model_name(model)
        response = self.api.call("POST", "/v2/repository/index", json={})
        self._check(response, "list the repository for", model)
        entries: list[dict[str, Any]] = [e for e in response.json() if e.get("name") == name]
        ready = sorted(
            {str(e.get("version", "")) for e in entries if e.get("state") == "READY"} - {""}
        )
        reasons = "; ".join(str(e["reason"]) for e in entries if e.get("reason"))
        if len(ready) == 1:
            return DeploymentState(model=model, version=ready[0], ready=True, detail=reasons)
        if not ready:
            loading = [e for e in entries if e.get("state") in ("LOADING", "UNLOADING")]
            return DeploymentState(model=model, version=None, ready=not loading, detail=reasons)
        return DeploymentState(model=model, version=ready[-1], ready=False,
                               detail=f"several versions ready: {ready}")

    def deploy(self, target: DeploymentTarget) -> None:
        self._load(target)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is not None:
            self._load(previous)
            return
        name = check_model_name(model)
        response = self.api.call("POST", f"/v2/repository/models/{name}/unload", json={})
        if response.status_code != 404:
            self._check(response, "unload", model)


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return TritonDeployment.from_settings(settings)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="triton",
        description="Triton / KServe v2 repository API (explicit model control); stages artifacts",
        features=frozenset({"undeploy", "needs_artifact"}),
        config_keys=(
            "triton_url", "triton_repository", "triton_base_config", "deployment_http_timeout_s",
        ),
        required_keys=("triton_url", "triton_repository"),
    ),
    factory=_build,
)
