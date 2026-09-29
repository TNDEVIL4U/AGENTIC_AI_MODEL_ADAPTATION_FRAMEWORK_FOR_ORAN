"""Deployment adapters over the Kubernetes API: ``kserve`` (an InferenceService per model),
``seldon`` (a Seldon Core v2 Model per model) and ``k8s`` (an existing Deployment per model).

All three talk to the API server over plain REST (K8S_API_URL, bearer token), with no
Kubernetes SDK. Each object carries the served version in the annotation
``oran.io/model-version``; readiness is read from the object's status and only counts once the
controller has observed the latest spec (``status.observedGeneration >= metadata.generation``),
so a status left over from the previous version is never mistaken for the new one.

Writes are read-modify-write with the object's ``resourceVersion``: a concurrent writer makes
the API answer 409, which fails the rollout (and restores the previous version) rather than
overwriting someone else's change.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from oran_adapt.adapters.deployment._common import (
    FileToken,
    HttpApi,
    StaticToken,
    check_template,
    dns_name,
    render,
    required,
)
from oran_adapt.core.errors import ConfigurationError, DeploymentError
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from collections.abc import Callable

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort

VERSION_ANNOTATION = "oran.io/model-version"
MODEL_ANNOTATION = "oran.io/model"
MANAGED_BY = {"app.kubernetes.io/managed-by": "oran-adapt"}
# KServe's status.modelStatus.transitionStatus values that mean the new spec will not load.
_KSERVE_FAILED = ("BlockedByFailedLoad", "InvalidSpec")
_K8S_KEYS = (
    "k8s_api_url", "k8s_namespace", "k8s_token", "k8s_token_file", "k8s_ca_file",
    "k8s_name_prefix", "deployment_http_timeout_s",
)


def kube_api(settings: Settings) -> HttpApi:
    token: Callable[[], str | None]
    if settings.k8s_token is not None:
        token = StaticToken(settings.k8s_token)
    elif settings.k8s_token_file:
        token = FileToken(settings.k8s_token_file)
    else:
        token = StaticToken(None)
    return HttpApi(
        required(settings.k8s_api_url, "k8s_api_url"),
        service="the Kubernetes API",
        http_factory=partial(
            httpx.Client,
            timeout=settings.deployment_http_timeout_s,
            verify=settings.k8s_ca_file or True,
        ),
        token=token,
    )


def _condition(obj: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for condition in obj.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == kind:
            return dict(condition)
    return None


def _observed(obj: dict[str, Any]) -> bool:
    observed = obj.get("status", {}).get("observedGeneration")
    return observed is not None and int(observed) >= int(obj["metadata"].get("generation", 0))


def _annotations(obj: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, Any] = obj.setdefault("metadata", {})
    annotations: dict[str, str] = metadata.setdefault("annotations", {}) or {}
    metadata["annotations"] = annotations
    return annotations


class _KubeObjectDeployment:
    """Shared GET / PUT / POST / DELETE of one namespaced object kind."""

    api_path: str  # e.g. /apis/serving.kserve.io/v1beta1
    plural: str

    def __init__(self, api: HttpApi, *, namespace: str, name_prefix: str) -> None:
        self.api = api
        self.namespace = namespace
        self.name_prefix = name_prefix

    def name(self, model: str) -> str:
        return dns_name(self.name_prefix, model)

    def _collection(self) -> str:
        return f"{self.api_path}/namespaces/{self.namespace}/{self.plural}"

    def _get(self, model: str) -> dict[str, Any] | None:
        response = self.api.call("GET", f"{self._collection()}/{self.name(model)}")
        if response.status_code == 404:
            return None
        self._check(response, "read", model)
        body: dict[str, Any] = response.json()
        return body

    def _check(self, response: httpx.Response, action: str, model: str) -> None:
        if response.status_code == 409:
            raise DeploymentError(
                f"another writer changed {self.plural}/{self.name(model)} during the {action}",
                model=model,
                cause=response.text[:500],
            )
        if response.status_code >= 400:
            raise DeploymentError(
                f"the Kubernetes API refused to {action} {self.plural}/{self.name(model)} "
                f"(HTTP {response.status_code})",
                model=model,
                cause=response.text[:500],
            )

    def _put(self, model: str, obj: dict[str, Any]) -> None:
        response = self.api.call("PUT", f"{self._collection()}/{self.name(model)}", json=obj)
        self._check(response, "update", model)

    def ping(self) -> None:
        response = self.api.call("GET", self._collection(), params={"limit": 1})
        self._check(response, "list", "_ping")


class _CustomResourceDeployment(_KubeObjectDeployment, ABC):
    """A model-serving custom resource this adapter creates, updates and deletes."""

    kind: str
    api_version: str
    ready_condition = "Ready"

    def __init__(self, api: HttpApi, *, namespace: str, name_prefix: str, uri_template: str) -> None:
        super().__init__(api, namespace=namespace, name_prefix=name_prefix)
        self.uri_template = uri_template

    @abstractmethod
    def _spec(self, obj: dict[str, Any], storage_uri: str) -> None:
        """Write the serving spec (storage URI and the kind's own fields) into ``obj``."""

    def _failed(self, obj: dict[str, Any]) -> bool:
        """Whether the controller reports the observed spec as failed for good. The base kind
        has no such signal: a rollout that never becomes ready fails on DEPLOYMENT_TIMEOUT_S."""
        return False

    def status(self, model: str) -> DeploymentState:
        obj = self._get(model)
        if obj is None:
            return DeploymentState(model=model, version=None, ready=True)
        if obj["metadata"].get("deletionTimestamp"):
            return DeploymentState(model=model, version=None, ready=False, detail="deleting")
        version = (obj["metadata"].get("annotations") or {}).get(VERSION_ANNOTATION)
        ready = _condition(obj, self.ready_condition) or {}
        return DeploymentState(
            model=model,
            version=version,
            ready=_observed(obj) and ready.get("status") == "True",
            failed=_observed(obj) and self._failed(obj),
            detail=str(ready.get("message") or ready.get("reason") or ""),
        )

    def deploy(self, target: DeploymentTarget) -> None:
        uri = render(self.uri_template, target, self.name(target.model))
        current = self._get(target.model)
        obj = copy.deepcopy(current) if current is not None else {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "metadata": {
                "name": self.name(target.model),
                "namespace": self.namespace,
                "labels": dict(MANAGED_BY),
            },
            "spec": {},
        }
        annotations = _annotations(obj)
        annotations[MODEL_ANNOTATION] = target.model
        annotations[VERSION_ANNOTATION] = target.version
        self._spec(obj, uri)
        if current is None:
            response = self.api.call("POST", self._collection(), json=obj)
            self._check(response, "create", target.model)
        else:
            self._put(target.model, obj)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is not None:
            self.deploy(previous)
            return
        response = self.api.call("DELETE", f"{self._collection()}/{self.name(model)}")
        if response.status_code != 404:
            self._check(response, "delete", model)


class KServeDeployment(_CustomResourceDeployment):
    api_path = "/apis/serving.kserve.io/v1beta1"
    api_version = "serving.kserve.io/v1beta1"
    plural = "inferenceservices"
    kind = "InferenceService"

    def __init__(self, api: HttpApi, *, namespace: str, name_prefix: str, uri_template: str,
                 model_format: str) -> None:
        super().__init__(api, namespace=namespace, name_prefix=name_prefix,
                         uri_template=uri_template)
        self.model_format = model_format

    def _spec(self, obj: dict[str, Any], storage_uri: str) -> None:
        model = obj["spec"].setdefault("predictor", {}).setdefault("model", {})
        model["modelFormat"] = {"name": self.model_format}
        model["storageUri"] = storage_uri

    def _failed(self, obj: dict[str, Any]) -> bool:
        transition = obj.get("status", {}).get("modelStatus", {}).get("transitionStatus")
        return transition in _KSERVE_FAILED


class SeldonDeployment(_CustomResourceDeployment):
    api_path = "/apis/mlops.seldon.io/v1alpha1"
    api_version = "mlops.seldon.io/v1alpha1"
    plural = "models"
    kind = "Model"

    def __init__(self, api: HttpApi, *, namespace: str, name_prefix: str, uri_template: str,
                 requirements: list[str]) -> None:
        super().__init__(api, namespace=namespace, name_prefix=name_prefix,
                         uri_template=uri_template)
        self.requirements = list(requirements)

    def _spec(self, obj: dict[str, Any], storage_uri: str) -> None:
        obj["spec"]["storageUri"] = storage_uri
        obj["spec"]["requirements"] = list(self.requirements)


class K8sDeployment(_KubeObjectDeployment):
    """An existing apps/v1 Deployment (created by whoever owns the serving manifests). The
    version goes into its pod template as the env var K8S_MODEL_ENV and an annotation, which
    rolls the pods; undeploying removes both again (the Deployment itself stays)."""

    api_path = "/apis/apps/v1"
    plural = "deployments"

    def __init__(self, api: HttpApi, *, namespace: str, name_prefix: str, env: str,
                 uri_template: str, container: str | None) -> None:
        super().__init__(api, namespace=namespace, name_prefix=name_prefix)
        self.env = env
        self.uri_template = uri_template
        self.container = container

    def _existing(self, model: str) -> dict[str, Any]:
        obj = self._get(model)
        if obj is None:
            raise DeploymentError(
                f"Deployment {self.namespace}/{self.name(model)} does not exist; the k8s "
                "adapter updates a Deployment, it does not create one",
                model=model,
            )
        return obj

    def _pod_container(self, obj: dict[str, Any], model: str) -> dict[str, Any]:
        containers: list[dict[str, Any]] = obj["spec"]["template"]["spec"]["containers"]
        if self.container is None:
            if len(containers) != 1:
                raise DeploymentError(
                    f"Deployment {self.name(model)} has {len(containers)} containers; set "
                    "K8S_CONTAINER to the one serving the model",
                    model=model,
                )
            return containers[0]
        for container in containers:
            if container.get("name") == self.container:
                return container
        raise DeploymentError(
            f"Deployment {self.name(model)} has no container {self.container!r}", model=model
        )

    def _set(self, model: str, target: DeploymentTarget | None) -> None:
        obj = self._existing(model)
        container = self._pod_container(obj, model)
        env = [e for e in container.get("env", []) or [] if e.get("name") != self.env]
        template_annotations = _annotations(obj["spec"]["template"])
        if target is None:
            template_annotations.pop(VERSION_ANNOTATION, None)
        else:
            env.append({"name": self.env,
                        "value": render(self.uri_template, target, self.name(model))})
            template_annotations[VERSION_ANNOTATION] = target.version
        container["env"] = env
        self._put(model, obj)

    def status(self, model: str) -> DeploymentState:
        obj = self._existing(model)
        annotations = obj["spec"]["template"].get("metadata", {}).get("annotations") or {}
        version = annotations.get(VERSION_ANNOTATION)
        status = obj.get("status", {})
        replicas = int(obj["spec"].get("replicas", 1))
        progressing = _condition(obj, "Progressing") or {}
        # Only once the controller observed this generation: the condition left from an
        # earlier failed rollout must not fail the restore that follows it.
        failed = _observed(obj) and progressing.get("reason") == "ProgressDeadlineExceeded"
        ready = (
            _observed(obj)
            and int(status.get("updatedReplicas", 0)) == replicas
            and int(status.get("availableReplicas", 0)) == replicas
            and int(status.get("readyReplicas", 0)) == replicas
            and int(status.get("replicas", 0)) == replicas  # old pods gone
        )
        return DeploymentState(
            model=model,
            version=version,
            ready=ready,
            failed=failed,
            detail=str(progressing.get("message", "")),
        )

    def deploy(self, target: DeploymentTarget) -> None:
        self._set(target.model, target)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        self._set(model, previous)


def _storage_template(key: str, value: str | None) -> str:
    """A storage-URI template must name the version: a spec that stays the same for a new
    version would not make the controller roll anything, yet read back as the new version."""
    template = check_template(key, required(value, key))
    if "{version}" not in template:
        raise ConfigurationError(f"{key.upper()} must contain {{version}}", key=key)
    return template


def _kserve(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return KServeDeployment(
        kube_api(settings),
        namespace=settings.k8s_namespace,
        name_prefix=settings.k8s_name_prefix,
        uri_template=_storage_template(
            "kserve_storage_uri_template", settings.kserve_storage_uri_template
        ),
        model_format=settings.kserve_model_format,
    )


def _seldon(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return SeldonDeployment(
        kube_api(settings),
        namespace=settings.k8s_namespace,
        name_prefix=settings.k8s_name_prefix,
        uri_template=_storage_template(
            "seldon_storage_uri_template", settings.seldon_storage_uri_template
        ),
        requirements=settings.seldon_requirements,
    )


def _k8s(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    if not settings.k8s_model_env:
        raise ConfigurationError("K8S_MODEL_ENV must name an env var", key="k8s_model_env")
    return K8sDeployment(
        kube_api(settings),
        namespace=settings.k8s_namespace,
        name_prefix=settings.k8s_name_prefix,
        env=settings.k8s_model_env,
        uri_template=check_template("k8s_model_uri_template", settings.k8s_model_uri_template),
        container=settings.k8s_container,
    )


KSERVE = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="kserve",
        description="a KServe InferenceService per model (serving.kserve.io/v1beta1)",
        features=frozenset({"undeploy", "kubernetes"}),
        config_keys=(*_K8S_KEYS, "kserve_storage_uri_template", "kserve_model_format"),
        required_keys=("k8s_api_url", "kserve_storage_uri_template"),
    ),
    factory=_kserve,
)

SELDON = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="seldon",
        description="a Seldon Core v2 Model per model (mlops.seldon.io/v1alpha1)",
        features=frozenset({"undeploy", "kubernetes"}),
        config_keys=(*_K8S_KEYS, "seldon_storage_uri_template", "seldon_requirements"),
        required_keys=("k8s_api_url", "seldon_storage_uri_template"),
    ),
    factory=_seldon,
)

K8S = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="k8s",
        description="an existing Kubernetes Deployment per model; version set in its pod template",
        features=frozenset({"kubernetes"}),
        config_keys=(*_K8S_KEYS, "k8s_model_env", "k8s_model_uri_template", "k8s_container"),
        required_keys=("k8s_api_url",),
    ),
    factory=_k8s,
)
