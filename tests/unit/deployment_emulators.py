"""In-memory emulators of the serving-system APIs the deployment adapters call.

- ``KubeEmulator``: the Kubernetes API server for namespaced objects (apps/v1 Deployments,
  KServe InferenceServices, Seldon Models) behind an httpx.MockTransport, with the semantics the
  adapters rely on: ``metadata.generation`` bumps only on a spec change, writes carry
  ``resourceVersion`` (a stale one gets 409), and a controller reconciles a changed object only
  after a few reads, so the adapters' read-back loops run.
- ``SagemakerEndpointEmulator``: the SageMaker registry emulator plus the real-time endpoint
  calls (CreateModel, CreateEndpointConfig, Create/Update/Describe/DeleteEndpoint), with
  Creating/Updating states and SageMaker's automatic rollback of a failed update.
- ``GitOpsController``: Argo CD / Flux plus the served status, reading what is committed.
- ``VertexEndpointEmulator``: the Vertex registry emulator plus endpoints, ``deployModel`` /
  ``undeployModel`` long-running operations and traffic-split updates.

Each has ``fail_next(...)`` (SageMaker: ``fail_endpoint``, since the registry emulator's
``fail_next`` already names a method to throttle) to make the next rollout of a model fail
the way that system reports failures. They are emulators, not the services:
conformance against them is "unverified against real systems" (docs/PHASE3_REPORT.md).

Emulators are registered in ``_LIVE`` and pickle by reference, so an adapter that went through
pickle (the conformance ``pickle`` check, worker processes) still talks to the same instance.
"""

from __future__ import annotations

import copy
import itertools
import json
import subprocess
from typing import Any

import httpx
import registry_emulators as emu
from registry_emulators import ClientError

K8S_TOKEN = "k8s-emulator-token"
SETTLE_AFTER = 2  # reads before a controller reconciles a change

_LIVE: dict[int, Any] = {}
_ids = itertools.count(1)


def _lookup(key: int) -> Any:
    return _LIVE[key]


class _Shared:
    """Pickles as a reference to the one live instance (same process only)."""

    def _share(self) -> None:
        self._key = next(_ids)
        _LIVE[self._key] = self

    def __reduce__(self) -> tuple[Any, tuple[int]]:
        return _lookup, (self._key,)


class MockClientFactory:
    """A picklable ``http_factory``: an httpx client wired to an emulator's ``handle``."""

    def __init__(self, emulator: Any) -> None:
        self.emulator = emulator

    def __call__(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.emulator.handle))


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


# ---- Kubernetes -------------------------------------------------------------------------


class KubeEmulator(_Shared):
    """``deployments_exist``: every apps/v1 Deployment a client reads exists (as if the serving
    manifests for every model were applied), which is the ``k8s`` adapter's precondition."""

    def __init__(self, *, deployments_exist: bool = False, settle_after: int = SETTLE_AFTER) -> None:
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}  # (ns, plural, name)
        self.pending: dict[tuple[str, str, str], int] = {}
        self.failing: set[str] = set()
        self.deployments_exist = deployments_exist
        self.settle_after = settle_after
        self.version = 0
        self.writes: list[tuple[str, str]] = []
        self._share()

    def fail_next(self, name: str) -> None:
        """The next reconcile of object ``name`` fails (the rollout never becomes ready)."""
        self.failing.add(name)

    def _rv(self) -> str:
        self.version += 1
        return str(self.version)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {K8S_TOKEN}":
            return _json(401, {"kind": "Status", "code": 401, "reason": "Unauthorized"})
        parts = request.url.path.strip("/").split("/")
        # apis/<group>/<version>/namespaces/<ns>/<plural>[/<name>]
        if len(parts) not in (6, 7) or parts[0] != "apis" or parts[3] != "namespaces":
            return _json(404, {"kind": "Status", "code": 404})
        namespace, plural = parts[4], parts[5]
        name = parts[6] if len(parts) == 7 else None
        if name is None:
            if request.method == "GET":
                items = [o for (ns, pl, _), o in self.objects.items()
                         if ns == namespace and pl == plural]
                return _json(200, {"items": copy.deepcopy(items)})
            if request.method == "POST":
                return self._create(namespace, plural, json.loads(request.content))
            return _json(405, {"code": 405})
        key = (namespace, plural, name)
        if request.method == "GET":
            return self._read(key)
        if request.method == "PUT":
            return self._replace(key, json.loads(request.content))
        if request.method == "DELETE":
            return self._delete(key)
        return _json(405, {"code": 405})

    def _create(self, namespace: str, plural: str, obj: dict[str, Any]) -> httpx.Response:
        key = (namespace, plural, obj["metadata"]["name"])
        if key in self.objects:
            return _json(409, {"kind": "Status", "code": 409, "reason": "AlreadyExists"})
        obj = copy.deepcopy(obj)
        obj["metadata"].update(generation=1, resourceVersion=self._rv(), namespace=namespace)
        obj["status"] = {}
        self.objects[key] = obj
        self.pending[key] = self.settle_after
        self.writes.append(("POST", key[2]))
        return _json(201, copy.deepcopy(obj))

    def _baseline(self, key: tuple[str, str, str]) -> dict[str, Any]:
        obj = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": key[2], "namespace": key[0], "generation": 1,
                         "resourceVersion": self._rv()},
            "spec": {"replicas": 2, "template": {"metadata": {}, "spec": {
                "containers": [{"name": "server", "image": "serve:1"}]}}},
            "status": {},
        }
        self._reconcile(key, obj)
        return obj

    def _read(self, key: tuple[str, str, str]) -> httpx.Response:
        if key not in self.objects and key[1] == "deployments" and self.deployments_exist:
            self.objects[key] = self._baseline(key)
        if key in self.pending:
            self.pending[key] -= 1
            if self.pending[key] <= 0:
                del self.pending[key]
                obj = self.objects[key]
                if obj["metadata"].get("deletionTimestamp"):
                    del self.objects[key]
                else:
                    self._reconcile(key, obj)
        if key not in self.objects:
            return _json(404, {"kind": "Status", "code": 404, "reason": "NotFound"})
        return _json(200, copy.deepcopy(self.objects[key]))

    def _replace(self, key: tuple[str, str, str], obj: dict[str, Any]) -> httpx.Response:
        current = self.objects.get(key)
        if current is None:
            return _json(404, {"kind": "Status", "code": 404, "reason": "NotFound"})
        if obj["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
            return _json(409, {"kind": "Status", "code": 409, "reason": "Conflict"})
        updated = copy.deepcopy(obj)
        updated["status"] = current.get("status", {})
        generation = int(current["metadata"]["generation"])
        if updated.get("spec") != current.get("spec"):
            generation += 1
            self.pending[key] = self.settle_after
        updated["metadata"].update(generation=generation, resourceVersion=self._rv())
        self.objects[key] = updated
        self.writes.append(("PUT", key[2]))
        return _json(200, copy.deepcopy(updated))

    def _delete(self, key: tuple[str, str, str]) -> httpx.Response:
        obj = self.objects.get(key)
        if obj is None:
            return _json(404, {"kind": "Status", "code": 404, "reason": "NotFound"})
        obj["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        self.pending[key] = self.settle_after
        self.writes.append(("DELETE", key[2]))
        return _json(200, {"kind": "Status", "status": "Success"})

    def _reconcile(self, key: tuple[str, str, str], obj: dict[str, Any]) -> None:
        name = key[2]
        failing = name in self.failing
        self.failing.discard(name)
        generation = obj["metadata"]["generation"]
        if key[1] == "deployments":
            replicas = int(obj["spec"].get("replicas", 1))
            if failing:
                obj["status"] = {
                    "observedGeneration": generation, "replicas": replicas + 1,
                    "updatedReplicas": 1, "readyReplicas": replicas,
                    "availableReplicas": replicas,
                    "conditions": [{"type": "Progressing", "status": "False",
                                    "reason": "ProgressDeadlineExceeded",
                                    "message": "ReplicaSet has timed out progressing."}],
                }
                return
            obj["status"] = {
                "observedGeneration": generation, "replicas": replicas,
                "updatedReplicas": replicas, "readyReplicas": replicas,
                "availableReplicas": replicas,
                "conditions": [
                    {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"},
                    {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
                ],
            }
            return
        ready = {"type": "Ready", "status": "False" if failing else "True"}
        status: dict[str, Any] = {"observedGeneration": generation, "conditions": [ready]}
        if key[1] == "inferenceservices":
            status["modelStatus"] = {
                "transitionStatus": "BlockedByFailedLoad" if failing else "UpToDate"
            }
            if failing:
                ready["reason"] = "PredictorFailed"
        obj["status"] = status


# ---- GitOps --------------------------------------------------------------------------------


class GitOpsController(_Shared):
    """A GitOps controller plus the serving system's status endpoint: it reads the manifest
    committed at HEAD of ``repo_dir`` (never the working tree), "applies" a changed one after a
    few status reads, and answers the webhook status contract (``GET ?model=``) with what it
    serves. ``fail_next(model)`` makes the next applied change of that model fail."""

    def __init__(self, repo_dir: str, manifest_path: str = "deployments/{name}.json",
                 settle_after: int = SETTLE_AFTER) -> None:
        self.repo_dir = repo_dir
        self.manifest_path = manifest_path
        self.settle_after = settle_after
        self.states: dict[str, dict[str, Any]] = {}
        self.failing: set[str] = set()
        self._share()

    def fail_next(self, model: str) -> None:
        self.failing.add(model)

    def _committed(self, model: str) -> str | None:
        from oran_adapt.adapters.deployment._common import dns_name
        from oran_adapt.core.errors import InvalidReferenceError

        try:
            name = dns_name("", model)
        except InvalidReferenceError:
            return None  # e.g. the adapter's "_ping" probe: no such model
        path = self.manifest_path.format(model=model, name=name)
        shown = subprocess.run(
            ["git", "show", f"HEAD:{path}"], cwd=self.repo_dir, capture_output=True, text=True,
            check=False,
        )
        if shown.returncode != 0:
            return None
        return str(json.loads(shown.stdout)["version"])

    def handle(self, request: httpx.Request) -> httpx.Response:
        model = request.url.params.get("model", "")
        desired = self._committed(model)
        state = self.states.setdefault(
            model, {"serving": None, "target": None, "polls": 0, "fail": False}
        )
        if desired != state["target"]:
            state.update(target=desired, polls=self.settle_after, fail=model in self.failing)
            self.failing.discard(model)
        if state["polls"] > 0:
            state["polls"] -= 1
            if state["polls"] == 0 and not state["fail"]:
                state["serving"] = state["target"]
        if state["polls"] == 0 and state["fail"]:
            return _json(200, {"version": state["target"], "ready": False, "failed": True,
                               "detail": "the new pods crash-loop"})
        if state["polls"] > 0:
            return _json(200, {"version": state["target"], "ready": False, "detail": "syncing"})
        if state["serving"] is None:
            return _json(404, {"error": f"model {model} is not deployed"})
        return _json(200, {"version": state["serving"], "ready": True})


# ---- SageMaker ---------------------------------------------------------------------------


def _missing(what: str) -> ClientError:
    return ClientError("ValidationException", f"Could not find {what}.")


class SagemakerEndpointEmulator(emu.SagemakerEmulator, _Shared):
    def __init__(self, settle_after: int = SETTLE_AFTER) -> None:
        super().__init__()
        self.models: dict[str, dict[str, Any]] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.failing: set[str] = set()
        self.settle_after = settle_after
        self._share()

    def fail_endpoint(self, endpoint: str) -> None:
        """The next create/update of ``endpoint`` fails (an update is rolled back)."""
        self.failing.add(endpoint)

    def _config_arn(self, name: str) -> str:
        return f"arn:aws:sagemaker:{emu.REGION}:{emu.ACCOUNT}:endpoint-config/{name}"

    def create_model(self, **kw: Any) -> dict[str, Any]:
        if kw["ModelName"] in self.models:
            raise ClientError("ValidationException", "Cannot create already existing model")
        if not kw.get("ExecutionRoleArn"):
            raise ClientError("ValidationException", "ExecutionRoleArn is required")
        for container in kw["Containers"]:
            self._package(container["ModelPackageName"])
        self.models[kw["ModelName"]] = copy.deepcopy(kw)
        return {"ModelArn": f"arn:aws:sagemaker:{emu.REGION}:{emu.ACCOUNT}:model/{kw['ModelName']}"}

    def create_endpoint_config(self, **kw: Any) -> dict[str, Any]:
        name = kw["EndpointConfigName"]
        if name in self.configs:
            raise ClientError("ValidationException", "Cannot create already existing config")
        for variant in kw["ProductionVariants"]:
            if variant["ModelName"] not in self.models:
                raise _missing(f"model {variant['ModelName']}")
        self.configs[name] = {
            "EndpointConfigName": name,
            "EndpointConfigArn": self._config_arn(name),
            "ProductionVariants": copy.deepcopy(kw["ProductionVariants"]),
            "tags": {t["Key"]: t["Value"] for t in kw.get("Tags", [])},
        }
        return {"EndpointConfigArn": self._config_arn(name)}

    def describe_endpoint_config(self, EndpointConfigName: str) -> dict[str, Any]:
        if EndpointConfigName not in self.configs:
            raise _missing(f"endpoint configuration {EndpointConfigName}")
        config = self.configs[EndpointConfigName]
        return {k: copy.deepcopy(v) for k, v in config.items() if k != "tags"}

    def list_tags(self, ResourceArn: str, NextToken: str | None = None) -> dict[str, Any]:
        for config in self.configs.values():
            if config["EndpointConfigArn"] == ResourceArn:
                tags = sorted(config["tags"].items())
                items, token = emu._page([{"Key": k, "Value": v} for k, v in tags], NextToken)
                return {"Tags": items, **({"NextToken": token} if token else {})}
        return super().list_tags(ResourceArn, NextToken)

    def create_endpoint(self, **kw: Any) -> dict[str, Any]:
        name = kw["EndpointName"]
        if name in self.endpoints:
            raise ClientError("ValidationException", "Cannot create already existing endpoint")
        if kw["EndpointConfigName"] not in self.configs:
            raise _missing(f"endpoint configuration {kw['EndpointConfigName']}")
        self.endpoints[name] = {"config": kw["EndpointConfigName"], "status": "Creating",
                                "pending": None, "polls": self.settle_after,
                                "fail": name in self.failing}
        self.failing.discard(name)
        return {"EndpointArn": f"arn:aws:sagemaker:{emu.REGION}:{emu.ACCOUNT}:endpoint/{name}"}

    def update_endpoint(self, EndpointName: str, EndpointConfigName: str) -> dict[str, Any]:
        endpoint = self.endpoints.get(EndpointName)
        if endpoint is None:
            raise _missing(f"endpoint {EndpointName}")
        if endpoint["status"] not in ("InService", "Failed"):
            raise ClientError("ValidationException",
                              f"Cannot update in-progress endpoint {EndpointName}")
        if EndpointConfigName not in self.configs:
            raise _missing(f"endpoint configuration {EndpointConfigName}")
        endpoint.update(status="Updating", pending=EndpointConfigName, polls=self.settle_after,
                        fail=EndpointName in self.failing)
        endpoint.pop("reason", None)
        self.failing.discard(EndpointName)
        return {"EndpointArn": f"arn:aws:sagemaker:{emu.REGION}:{emu.ACCOUNT}:endpoint/{EndpointName}"}

    def describe_endpoint(self, EndpointName: str) -> dict[str, Any]:
        endpoint = self.endpoints.get(EndpointName)
        if endpoint is None:
            raise _missing(f"endpoint {EndpointName}")
        if endpoint["status"] in ("Creating", "Updating"):
            endpoint["polls"] -= 1
            if endpoint["polls"] <= 0:
                if not endpoint["fail"]:
                    if endpoint["pending"]:
                        endpoint["config"] = endpoint["pending"]
                    endpoint["status"] = "InService"
                elif endpoint["status"] == "Creating":
                    endpoint.update(status="Failed", reason="the model container failed to start")
                else:  # SageMaker rolls a failed update back to the running config
                    endpoint.update(status="InService",
                                    reason="update failed; rolled back to the previous config")
                endpoint["pending"] = None
        body = {"EndpointName": EndpointName, "EndpointConfigName": endpoint["config"],
                "EndpointStatus": endpoint["status"]}
        if "reason" in endpoint:
            body["FailureReason"] = endpoint["reason"]
        return body

    def delete_endpoint(self, EndpointName: str) -> dict[str, Any]:
        if EndpointName not in self.endpoints:
            raise _missing(f"endpoint {EndpointName}")
        del self.endpoints[EndpointName]
        return {}

    def list_endpoints(self, MaxResults: int) -> dict[str, Any]:
        self._maybe_fail("list_endpoints")
        return {"Endpoints": [{"EndpointName": n} for n in sorted(self.endpoints)][:MaxResults]}


# ---- Vertex AI -----------------------------------------------------------------------------


class VertexEndpointEmulator(emu.VertexEmulator, _Shared):
    def __init__(self, project: str, location: str, bucket: str) -> None:
        super().__init__(project, location, bucket)
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.endpoint_ops: dict[str, dict[str, Any]] = {}
        self.failing: set[str] = set()  # model ids whose next deployModel fails
        self.deployed_ids = itertools.count(1000)
        self._share()

    def fail_next(self, model_id: str) -> None:
        self.failing.add(model_id)

    def _vertex(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        name = path[len("/v1/"):]
        if name in self.endpoint_ops:
            return self._endpoint_op(name)
        collection = f"/v1/{self.parent}/endpoints"
        if path == collection:
            if request.method == "GET":
                return _json(200, {"endpoints": copy.deepcopy(list(self.endpoints.values()))})
            if request.method == "POST":
                return self._create_endpoint(request)
        if path.startswith(collection + "/"):
            ref, _, verb = path[len(collection) + 1:].partition(":")
            return self._endpoint(request, ref, verb)
        return super()._vertex(request)

    def _op(self, effect: Any) -> httpx.Response:
        name = f"{self.parent}/operations/ep-{len(self.endpoint_ops) + 1}"
        self.endpoint_ops[name] = {"polls": 0, "effect": effect}
        return _json(200, {"name": name, "done": False})

    def _endpoint_op(self, name: str) -> httpx.Response:
        op = self.endpoint_ops[name]
        op["polls"] += 1
        if op["polls"] < SETTLE_AFTER:
            return _json(200, {"name": name, "done": False})
        if "result" not in op:
            op["result"] = op["effect"]()
        error = op["result"]
        if error:
            return _json(200, {"name": name, "done": True, "error": {"code": 9, "message": error}})
        return _json(200, {"name": name, "done": True, "response": {}})

    def _create_endpoint(self, request: httpx.Request) -> httpx.Response:
        endpoint_id = request.url.params.get("endpointId", "")
        if endpoint_id in self.endpoints:
            return _json(409, {"error": {"code": 409, "status": "ALREADY_EXISTS"}})
        body = json.loads(request.content)

        def create() -> str | None:
            self.endpoints[endpoint_id] = {
                "name": f"{self.parent}/endpoints/{endpoint_id}",
                "displayName": body["displayName"], "deployedModels": [], "trafficSplit": {},
            }
            return None

        return self._op(create)

    def _endpoint(self, request: httpx.Request, ref: str, verb: str) -> httpx.Response:
        endpoint = self.endpoints.get(ref)
        if endpoint is None:
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if not verb and request.method == "GET":
            return _json(200, copy.deepcopy(endpoint))
        if not verb and request.method == "PATCH":
            if request.url.params.get("updateMask") != "traffic_split":
                return _json(400, {"error": {"code": 400, "message": "unsupported updateMask"}})
            split = {k: int(v) for k, v in json.loads(request.content)["trafficSplit"].items()}
            ids = {d["id"] for d in endpoint["deployedModels"]}
            if sum(split.values()) != 100 or not set(split) <= ids:
                return _json(400, {"error": {"code": 400, "message": "invalid traffic split"}})
            endpoint["trafficSplit"] = split
            return _json(200, copy.deepcopy(endpoint))
        body = json.loads(request.content)
        if verb == "deployModel" and request.method == "POST":
            return self._deploy_model(endpoint, body)
        if verb == "undeployModel" and request.method == "POST":
            return self._undeploy_model(endpoint, body["deployedModelId"])
        return _json(404, {"error": {"code": 404}})

    def _deploy_model(self, endpoint: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        resource, _, version_id = body["deployedModel"]["model"].partition("@")
        mid = resource.rsplit("/", 1)[1]
        model = self.models.get(mid)
        if model is None or not any(str(v["versionId"]) == version_id
                                    for v in model["versions"]):
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        split = {k: int(v) for k, v in body.get("trafficSplit", {}).items()}
        if sum(split.values()) != 100:
            return _json(400, {"error": {"code": 400, "message": "traffic split must sum to 100"}})
        failing = mid in self.failing
        self.failing.discard(mid)

        def deploy() -> str | None:
            if failing:
                return "the model server failed to become healthy"
            deployed_id = str(next(self.deployed_ids))
            endpoint["deployedModels"].append({
                "id": deployed_id, "model": resource, "modelVersionId": version_id,
                "displayName": body["deployedModel"]["displayName"],
            })
            endpoint["trafficSplit"] = {
                **{d["id"]: 0 for d in endpoint["deployedModels"]},
                **{(deployed_id if k == "0" else k): v for k, v in split.items()},
            }
            return None

        return self._op(deploy)

    def _undeploy_model(self, endpoint: dict[str, Any], deployed_id: str) -> httpx.Response:
        ids = [d["id"] for d in endpoint["deployedModels"]]
        if deployed_id not in ids:
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if endpoint["trafficSplit"].get(deployed_id, 0) > 0 and len(ids) > 1:
            return _json(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                         "message": "model carries traffic"}})

        def undeploy() -> str | None:
            endpoint["deployedModels"] = [
                d for d in endpoint["deployedModels"] if d["id"] != deployed_id
            ]
            endpoint["trafficSplit"].pop(deployed_id, None)
            return None

        return self._op(undeploy)
