"""Deployment adapter ``vertex``: a Vertex AI endpoint per model, serving versions of the
``vertex`` registry adapter's models, over the REST API.

Mapping: the endpoint id is the model name made DNS-safe after VERTEX_ENDPOINT_PREFIX; the
endpoint is created on first deploy. A rollout calls ``:deployModel`` for
``models/<id>@<version>`` with all traffic (the previous deployed model drops to 0 % and stays
deployed, so a rollback is a traffic switch, not a new deployment); deployed models left at 0 %
by the rollout before are undeployed first. ``status`` reads the endpoint's deployed models and
traffic split (the version with 100 % = settled) and, while a ``:deployModel`` operation this
instance started is running, reports it as not settled, or failed when the operation failed.
"""

from __future__ import annotations

import json
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from oran_adapt.adapters.deployment._common import HttpApi, dns_name, required
from oran_adapt.adapters.registry.vertex import AdcToken, model_id
from oran_adapt.core.errors import ConfigurationError, DeploymentError
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from collections.abc import Callable

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class VertexDeployment:
    def __init__(
        self,
        api: HttpApi,
        *,
        project: str,
        location: str,
        endpoint_prefix: str,
        machine_type: str,
        min_replicas: int,
        max_replicas: int,
        operation_timeout_s: float,
        operation_poll_s: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_replicas < min_replicas:
            raise ConfigurationError(
                "VERTEX_MAX_REPLICAS must be at least VERTEX_MIN_REPLICAS", key="vertex_max_replicas"
            )
        self.api = api
        self.parent = f"projects/{project}/locations/{location}"
        self.endpoint_prefix = endpoint_prefix
        self.machine_type = machine_type
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        self.operation_timeout_s = operation_timeout_s
        self.operation_poll_s = operation_poll_s
        self._sleep = sleep
        self._pending: dict[str, tuple[str, str]] = {}  # model -> (operation name, version)

    @classmethod
    def from_settings(cls, settings: Settings) -> VertexDeployment:
        if "vertex" not in (settings.registry_backend, settings.registry_mirror_primary):
            raise ConfigurationError(
                "DEPLOYMENT_BACKEND=vertex serves model versions of the vertex registry; set "
                "REGISTRY_BACKEND=vertex (or a mirror whose primary is vertex)",
                key="deployment_backend",
            )
        location = required(settings.vertex_location, "vertex_location")
        endpoint = settings.vertex_api_endpoint or f"https://{location}-aiplatform.googleapis.com"
        return cls(
            HttpApi(
                f"{endpoint.rstrip('/')}/v1",
                service="Vertex AI",
                http_factory=partial(
                    OutboundPolicy.from_settings(settings).client,
                    timeout=settings.deployment_http_timeout_s,
                ),
                token=AdcToken(),
            ),
            project=required(settings.vertex_project, "vertex_project"),
            location=location,
            endpoint_prefix=settings.vertex_endpoint_prefix,
            machine_type=settings.vertex_machine_type,
            min_replicas=settings.vertex_min_replicas,
            max_replicas=settings.vertex_max_replicas,
            operation_timeout_s=settings.vertex_operation_timeout_s,
            operation_poll_s=settings.vertex_operation_poll_s,
        )

    def __getstate__(self) -> dict[str, Any]:
        return {**self.__dict__, "_pending": {}}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)

    # ---- REST --------------------------------------------------------------------------

    def _json(self, method: str, path: str, *, missing_ok: bool = False,
              **kwargs: Any) -> dict[str, Any] | None:
        response = self.api.call(method, path, **kwargs)
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 400:
            raise DeploymentError(
                f"Vertex AI refused {method} {path} (HTTP {response.status_code})",
                cause=response.text[:500],
            )
        body: dict[str, Any] = response.json()
        return body

    def _wait(self, operation: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.operation_timeout_s
        while not operation.get("done"):
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    "a Vertex AI operation did not finish within VERTEX_OPERATION_TIMEOUT_S",
                    operation=operation.get("name"),
                )
            self._sleep(self.operation_poll_s)
            operation = self._json("GET", f"/{operation['name']}") or {}
        if "error" in operation:
            raise DeploymentError(
                "a Vertex AI operation failed", cause=json.dumps(operation["error"])[:500]
            )

    def endpoint_path(self, model: str) -> str:
        return f"{self.parent}/endpoints/{dns_name(self.endpoint_prefix, model)}"

    def _endpoint(self, model: str) -> dict[str, Any] | None:
        return self._json("GET", f"/{self.endpoint_path(model)}", missing_ok=True)

    def _model_resource(self, model: str) -> str:
        return f"{self.parent}/models/{model_id(model)}"

    def _undeploy(self, model: str, deployed_id: str) -> None:
        operation = self._json(
            "POST", f"/{self.endpoint_path(model)}:undeployModel",
            json={"deployedModelId": deployed_id},
        )
        self._wait(operation or {})

    # ---- port --------------------------------------------------------------------------

    def ping(self) -> None:
        self._json("GET", f"/{self.parent}/endpoints", params={"pageSize": 1})

    def status(self, model: str) -> DeploymentState:
        pending = self._pending.get(model)
        if pending is not None:
            name, version = pending
            operation = self._json("GET", f"/{name}") or {}
            if not operation.get("done"):
                return DeploymentState(model=model, version=version, ready=False,
                                       detail="deployModel running")
            del self._pending[model]
            if "error" in operation:
                return DeploymentState(model=model, version=version, ready=False, failed=True,
                                       detail=json.dumps(operation["error"])[:500])
        endpoint = self._endpoint(model)
        if endpoint is None:
            return DeploymentState(model=model, version=None, ready=True)
        mine = self._model_resource(model)
        deployed = [d for d in endpoint.get("deployedModels", []) if d.get("model") == mine]
        if not deployed:
            return DeploymentState(model=model, version=None, ready=True)
        traffic = {str(k): int(v) for k, v in (endpoint.get("trafficSplit") or {}).items()}
        full = [d for d in deployed if traffic.get(str(d["id"]), 0) == 100]
        if len(full) == 1:
            return DeploymentState(model=model, version=str(full[0]["modelVersionId"]), ready=True)
        top = max(deployed, key=lambda d: traffic.get(str(d["id"]), 0))
        return DeploymentState(model=model, version=str(top["modelVersionId"]), ready=False,
                               detail=f"traffic split {traffic}")

    def deploy(self, target: DeploymentTarget) -> None:
        path = self.endpoint_path(target.model)
        endpoint = self._endpoint(target.model)
        if endpoint is None:
            operation = self._json(
                "POST", f"/{self.parent}/endpoints",
                params={"endpointId": dns_name(self.endpoint_prefix, target.model)},
                json={"displayName": target.model},
            )
            self._wait(operation or {})
            endpoint = {}
        traffic = {str(k): int(v) for k, v in (endpoint.get("trafficSplit") or {}).items()}
        for stale in endpoint.get("deployedModels", []):
            if traffic.get(str(stale["id"]), 0) == 0:
                self._undeploy(target.model, str(stale["id"]))
        operation = self._json(
            "POST", f"/{path}:deployModel",
            json={
                "deployedModel": {
                    "model": f"{self._model_resource(target.model)}@{target.version}",
                    "displayName": f"{dns_name('', target.model)}-v{target.version}",
                    "dedicatedResources": {
                        "machineSpec": {"machineType": self.machine_type},
                        "minReplicaCount": self.min_replicas,
                        "maxReplicaCount": self.max_replicas,
                    },
                },
                "trafficSplit": {"0": 100},
            },
        ) or {}
        self._pending[target.model] = (str(operation["name"]), target.version)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        self._pending.pop(model, None)
        endpoint = self._endpoint(model)
        if endpoint is None:
            if previous is not None:
                self.deploy(previous)
            return
        mine = self._model_resource(model)
        deployed = [d for d in endpoint.get("deployedModels", []) if d.get("model") == mine]
        if previous is None:
            # Zero-traffic models first: Vertex refuses to undeploy a model that carries
            # traffic while another one is still deployed.
            traffic = {str(k): int(v) for k, v in (endpoint.get("trafficSplit") or {}).items()}
            for d in sorted(deployed, key=lambda d: traffic.get(str(d["id"]), 0)):
                self._undeploy(model, str(d["id"]))
            return
        match = [d for d in deployed if str(d["modelVersionId"]) == previous.version]
        if not match:
            self.deploy(previous)
            return
        split = {str(d["id"]): 0 for d in endpoint.get("deployedModels", [])}
        split[str(match[0]["id"])] = 100
        self._json(
            "PATCH", f"/{self.endpoint_path(model)}",
            params={"updateMask": "traffic_split"},
            json={"trafficSplit": split},
        )


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return VertexDeployment.from_settings(settings)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="vertex",
        description="a Vertex AI endpoint per model, serving vertex registry versions (REST)",
        features=frozenset({"undeploy", "cloud", "traffic_switch_rollback"}),
        config_keys=(
            "vertex_project", "vertex_location", "vertex_api_endpoint", "vertex_endpoint_prefix",
            "vertex_machine_type", "vertex_min_replicas", "vertex_max_replicas",
            "vertex_operation_timeout_s", "vertex_operation_poll_s", "deployment_http_timeout_s",
        ),
        required_keys=("vertex_project", "vertex_location"),
        distributions=("google-auth",),
    ),
    factory=_build,
)
