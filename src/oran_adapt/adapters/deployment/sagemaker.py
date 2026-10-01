"""Deployment adapter ``sagemaker``: a SageMaker real-time endpoint per model, serving the
model packages the ``sagemaker`` registry adapter registers.

Mapping: the endpoint is named like the model's package group (SAGEMAKER_ENDPOINT_PREFIX +
the group name rules of ``adapters.registry.sagemaker.group_name``). A rollout creates a
SageMaker Model from the version's model package (``ExecutionRoleArn`` =
SAGEMAKER_DEPLOY_ROLE_ARN) and an endpoint config with one variant, tagged
``oran:model-version``, then creates or updates the endpoint. ``status`` reads the endpoint
(``InService`` = settled; ``Failed`` = failed) and the version tag of the endpoint config it
runs. SageMaker rolls a failed update back by itself; the endpoint then settles at the old
version, which the core reads as a failed rollout. Undeploying deletes the endpoint (models
and endpoint configs are kept: they cost nothing and record what ran).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from oran_adapt.adapters.deployment._common import required
from oran_adapt.adapters.registry.sagemaker import _error, _is_missing, group_name
from oran_adapt.core.errors import (
    ConfigurationError,
    DeploymentError,
    DeploymentUnavailableError,
    ModelNotFoundError,
)
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort

VERSION_TAG = "oran:model-version"
MODEL_TAG = "oran:model-name"
_NAME_MAX = 63
_SETTLED = "InService"
_FAILED = "Failed"
# A 4xx that means the request itself was refused (bad role, quota), not an outage.
_REFUSED = ("ValidationException", "ResourceLimitExceeded", "ResourceInUse", "AccessDenied")


class _Missing(Exception):
    pass


class SagemakerDeployment:
    def __init__(
        self,
        *,
        role_arn: str,
        instance_type: str,
        instance_count: int,
        group_prefix: str,
        endpoint_prefix: str,
        sm_client: Any,
    ) -> None:
        self.role_arn = role_arn
        self.instance_type = instance_type
        self.instance_count = instance_count
        self.group_prefix = group_prefix
        self.endpoint_prefix = endpoint_prefix
        self.sm = sm_client
        self._settings: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> SagemakerDeployment:
        if "sagemaker" not in (settings.registry_backend, settings.registry_mirror_primary):
            raise ConfigurationError(
                "DEPLOYMENT_BACKEND=sagemaker serves model packages of the sagemaker registry; "
                "set REGISTRY_BACKEND=sagemaker (or a mirror whose primary is sagemaker)",
                key="deployment_backend",
            )
        import boto3

        region = required(settings.sagemaker_region, "sagemaker_region")
        deployment = cls(
            role_arn=required(settings.sagemaker_deploy_role_arn, "sagemaker_deploy_role_arn"),
            instance_type=settings.sagemaker_instance_type,
            instance_count=settings.sagemaker_instance_count,
            group_prefix=settings.sagemaker_group_prefix,
            endpoint_prefix=settings.sagemaker_endpoint_prefix,
            sm_client=boto3.client(
                "sagemaker", region_name=region, endpoint_url=settings.sagemaker_endpoint_url
            ),
        )
        deployment._settings = {"region": region, "endpoint_url": settings.sagemaker_endpoint_url}
        return deployment

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        if self._settings is not None:
            state["sm"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        if self._settings is not None:
            import boto3

            self.sm = boto3.client(
                "sagemaker",
                region_name=self._settings["region"],
                endpoint_url=self._settings["endpoint_url"],
            )

    def _call(self, method: str, **kwargs: Any) -> Any:
        try:
            return getattr(self.sm, method)(**kwargs)
        except Exception as exc:
            if _is_missing(exc):
                raise _Missing(_error(exc)[1]) from exc
            code, message = _error(exc)
            if code in _REFUSED:
                raise DeploymentError(
                    f"SageMaker refused {method}", code=code, cause=message
                ) from exc
            raise DeploymentUnavailableError(
                f"SageMaker {method} failed", code=code, cause=message
            ) from exc

    def endpoint(self, model: str) -> str:
        return group_name(self.endpoint_prefix + self.group_prefix, model)

    def _package_arn(self, model: str, version: str) -> str:
        group = group_name(self.group_prefix, model)
        try:
            described = self._call("describe_model_package_group", ModelPackageGroupName=group)
        except _Missing as exc:
            raise ModelNotFoundError(f"Model '{model}' not found", model=model) from exc
        arn = str(described["ModelPackageGroupArn"])
        return f"{arn.replace(':model-package-group/', ':model-package/')}/{version}"

    def ping(self) -> None:
        self._call("list_endpoints", MaxResults=1)

    def status(self, model: str) -> DeploymentState:
        try:
            endpoint = self._call("describe_endpoint", EndpointName=self.endpoint(model))
        except _Missing:
            return DeploymentState(model=model, version=None, ready=True)
        state = str(endpoint.get("EndpointStatus", ""))
        try:
            config = self._call(
                "describe_endpoint_config", EndpointConfigName=endpoint["EndpointConfigName"]
            )
            tags = self._call("list_tags", ResourceArn=config["EndpointConfigArn"])
        except _Missing:
            return DeploymentState(model=model, version=None, ready=False,
                                   detail="the endpoint's config is missing")
        version = {t["Key"]: t["Value"] for t in tags.get("Tags", [])}.get(VERSION_TAG)
        return DeploymentState(
            model=model,
            version=version,
            ready=state == _SETTLED,
            failed=state == _FAILED,
            detail=str(endpoint.get("FailureReason", state)),
        )

    def deploy(self, target: DeploymentTarget) -> None:
        endpoint = self.endpoint(target.model)
        package = self._package_arn(target.model, target.version)
        suffix = f"-v{target.version}-{uuid.uuid4().hex[:8]}"
        name = endpoint[: _NAME_MAX - len(suffix)].rstrip("-") + suffix
        tags = [{"Key": VERSION_TAG, "Value": target.version},
                {"Key": MODEL_TAG, "Value": target.model}]
        self._call(
            "create_model",
            ModelName=name,
            ExecutionRoleArn=self.role_arn,
            Containers=[{"ModelPackageName": package}],
            Tags=tags,
        )
        self._call(
            "create_endpoint_config",
            EndpointConfigName=name,
            ProductionVariants=[{
                "VariantName": "primary",
                "ModelName": name,
                "InstanceType": self.instance_type,
                "InitialInstanceCount": self.instance_count,
            }],
            Tags=tags,
        )
        try:
            self._call("describe_endpoint", EndpointName=endpoint)
        except _Missing:
            self._call("create_endpoint", EndpointName=endpoint, EndpointConfigName=name,
                       Tags=[{"Key": MODEL_TAG, "Value": target.model}])
            return
        self._call("update_endpoint", EndpointName=endpoint, EndpointConfigName=name)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is not None:
            self.deploy(previous)
            return
        try:
            self._call("delete_endpoint", EndpointName=self.endpoint(model))
        except _Missing:
            return


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return SagemakerDeployment.from_settings(settings)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="sagemaker",
        description="a SageMaker real-time endpoint per model, from sagemaker registry packages",
        features=frozenset({"undeploy", "cloud"}),
        config_keys=(
            "sagemaker_region", "sagemaker_endpoint_url", "sagemaker_group_prefix",
            "sagemaker_deploy_role_arn", "sagemaker_instance_type", "sagemaker_instance_count",
            "sagemaker_endpoint_prefix",
        ),
        required_keys=("sagemaker_region", "sagemaker_deploy_role_arn"),
        distributions=("boto3",),
    ),
    factory=_build,
)
