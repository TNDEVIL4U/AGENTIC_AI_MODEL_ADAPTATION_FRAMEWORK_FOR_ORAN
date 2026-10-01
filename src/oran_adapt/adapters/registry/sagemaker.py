"""Registry adapter ``sagemaker``: the Amazon SageMaker Model Registry.

Mapping:

- model name -> Model Package Group ``SAGEMAKER_GROUP_PREFIX + sanitized name``. Group names
  allow only letters, digits and hyphens (63 at most), so ``_`` and ``.`` become ``-`` and an
  over-long name is shortened with a hash suffix. The original name is kept in the group tag
  ``oran:model-name``; a group that already carries a different name is a ConflictError, so
  two names that sanitize alike can never share a group.
- version -> ModelPackageVersion (numbered 1, 2, ... by SageMaker).
- version tags -> CustomerMetadataProperties (keys starting ``oran:`` are the adapter's own).
- aliases -> group tags ``oran-alias:<alias>`` holding the version.
- artifacts -> ``model.tar.gz`` in S3 (SAGEMAKER_S3_BUCKET / SAGEMAKER_S3_PREFIX), referenced as
  the ModelDataUrl of the InferenceSpecification (image SAGEMAKER_INFERENCE_IMAGE); metrics and
  lineage are JSON files next to it.

boto3 is imported only when the adapter is built from settings; ``sm_client`` and
``s3_client`` can be injected (tests use an emulator of the calls made here).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import (
    ArtifactError,
    ConflictError,
    ModelNotFoundError,
    RegistryUnavailableError,
    UnsupportedAdaptationError,
)
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.ports import AdapterSpec, Capability
from oran_adapt.ports.registry import FAILED, PENDING, READY, ModelVersion, RegisteredModel

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.core.config import Settings

NAME_TAG = "oran:model-name"
ALIAS_TAG = "oran-alias:"
_OWN = "oran:"
_METRICS_PROP = "oran:metrics-uri"
_LINEAGE_PROP = "oran:lineage-uri"
_GROUP_MAX = 63
_GROUP_RE = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
_PROP_KEY_RE = re.compile(r"^[\w\s.:/=+\-@]{1,128}$")
_PROP_VALUE_MAX = 256
_PROP_COUNT_MAX = 50
_STATUS = {
    "Completed": READY,
    "Pending": PENDING,
    "InProgress": PENDING,
    "Failed": FAILED,
    "Deleting": FAILED,
}
_MISSING = ("does not exist", "not found", "could not find")


def group_name(prefix: str, name: str) -> str:
    """The Model Package Group holding model ``name``."""
    base = prefix + re.sub(r"[^A-Za-z0-9-]", "-", check_model_name(name))
    if _GROUP_RE.match(base) and base == prefix + name:
        return base
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    head = base[: _GROUP_MAX - len(digest) - 1].strip("-")
    return f"{head}-{digest}"


def _error(exc: Exception) -> tuple[str, str]:
    error = getattr(exc, "response", {}).get("Error", {})
    return str(error.get("Code", "")), str(error.get("Message", str(exc)))


def _is_missing(exc: Exception) -> bool:
    code, message = _error(exc)
    return code in ("ResourceNotFound", "NoSuchKey", "404") or (
        code == "ValidationException" and any(m in message.lower() for m in _MISSING)
    )


class _Missing(Exception):
    """The addressed SageMaker or S3 resource does not exist."""


class SagemakerRegistry:
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        inference_image: str,
        group_prefix: str,
        content_types: Sequence[str],
        artifact_policy: ArtifactPolicy,
        sm_client: Any,
        s3_client: Any,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.inference_image = inference_image
        self.group_prefix = group_prefix
        self.content_types = list(content_types)
        self.artifact_policy = artifact_policy
        self.sm = sm_client
        self.s3 = s3_client
        self._settings: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> SagemakerRegistry:
        import boto3

        client_args = {
            "region_name": settings.sagemaker_region,
            "endpoint_url": settings.sagemaker_endpoint_url,
        }
        registry = cls(
            bucket=str(settings.sagemaker_s3_bucket),
            prefix=settings.sagemaker_s3_prefix,
            inference_image=str(settings.sagemaker_inference_image),
            group_prefix=settings.sagemaker_group_prefix,
            content_types=settings.sagemaker_content_types,
            artifact_policy=ArtifactPolicy.from_settings(settings),
            sm_client=boto3.client("sagemaker", **client_args),
            s3_client=boto3.client("s3", region_name=settings.sagemaker_region),
        )
        registry._settings = {
            "region": settings.sagemaker_region,
            "endpoint_url": settings.sagemaker_endpoint_url,
        }
        return registry

    # A job worker process gets fresh boto3 clients (built from the region); injected clients
    # travel with the instance.
    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        if self._settings is not None:
            state["sm"] = state["s3"] = None
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
            self.s3 = boto3.client("s3", region_name=self._settings["region"])

    # ---- helpers -----------------------------------------------------------------------

    def _call(self, client: Any, method: str, **kwargs: Any) -> Any:
        try:
            return getattr(client, method)(**kwargs)
        except Exception as exc:
            if _is_missing(exc):
                raise _Missing(_error(exc)[1]) from exc
            code, message = _error(exc)
            raise RegistryUnavailableError(
                f"SageMaker {method} failed", code=code, cause=message
            ) from exc

    def _group(self, name: str) -> dict[str, Any]:
        """The model's group description; ModelNotFoundError when there is none."""
        group = group_name(self.group_prefix, name)
        try:
            described: dict[str, Any] = self._call(
                self.sm, "describe_model_package_group", ModelPackageGroupName=group
            )
        except _Missing as exc:
            raise ModelNotFoundError(f"Model '{name}' not found", model=name) from exc
        tags = self._group_tags(described["ModelPackageGroupArn"])
        if tags.get(NAME_TAG) != name:
            raise ModelNotFoundError(
                f"Model '{name}' not found; its group {group!r} belongs to "
                f"{tags.get(NAME_TAG)!r}",
                model=name,
            )
        described["_tags"] = tags
        return described

    def _group_tags(self, arn: str) -> dict[str, str]:
        tags: dict[str, str] = {}
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"ResourceArn": arn}
            if token:
                kwargs["NextToken"] = token
            page = self._call(self.sm, "list_tags", **kwargs)
            tags.update({t["Key"]: t["Value"] for t in page.get("Tags", [])})
            token = page.get("NextToken")
            if not token:
                return tags

    @staticmethod
    def _package_arn(group_arn: str, version: str) -> str:
        return f"{group_arn.replace(':model-package-group/', ':model-package/')}/{version}"

    def _package(self, name: str, version: str) -> dict[str, Any]:
        group = self._group(name)
        if not version.isdigit():
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            )
        try:
            package: dict[str, Any] = self._call(
                self.sm,
                "describe_model_package",
                ModelPackageName=self._package_arn(group["ModelPackageGroupArn"], version),
            )
        except _Missing as exc:
            raise ModelNotFoundError(
                f"Model '{name}' version '{version}' not found", model=name, version=version
            ) from exc
        return package

    @staticmethod
    def _version(name: str, package: dict[str, Any]) -> ModelVersion:
        props = package.get("CustomerMetadataProperties") or {}
        created = package.get("CreationTime")
        return ModelVersion(
            name=name,
            version=str(package["ModelPackageVersion"]),
            tags={k: v for k, v in props.items() if not k.startswith(_OWN)},
            status=_STATUS.get(str(package.get("ModelPackageStatus")), PENDING),
            created_at_ms=int(created.timestamp() * 1000) if created is not None else None,
            source=package.get("ModelPackageArn"),
        )

    @staticmethod
    def _check_properties(tags: dict[str, str]) -> dict[str, str]:
        clean = {k: str(v) for k, v in tags.items()}
        for key, value in clean.items():
            if key.startswith(_OWN) or not _PROP_KEY_RE.match(key):
                raise UnsupportedAdaptationError(
                    f"tag key {key!r} cannot be stored as a SageMaker metadata property",
                    key=key,
                )
            if len(value) > _PROP_VALUE_MAX:
                raise UnsupportedAdaptationError(
                    f"tag {key!r} is longer than SageMaker's {_PROP_VALUE_MAX} characters",
                    key=key,
                    length=len(value),
                )
        return clean

    def _s3_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _s3_key(self, uri: str) -> str:
        head = f"s3://{self.bucket}/"
        if not uri.startswith(head):
            raise ArtifactError("artifact is outside the configured bucket", uri=uri)
        return uri[len(head) :]

    def _put_json(self, key: str, data: Any) -> str:
        body = json.dumps(data, sort_keys=True).encode()
        self._call(self.s3, "put_object", Bucket=self.bucket, Key=key, Body=body)
        return self._s3_uri(key)

    # ---- port --------------------------------------------------------------------------

    def ping(self) -> None:
        try:
            self._call(self.sm, "list_model_package_groups", MaxResults=1)
            self._call(self.s3, "head_bucket", Bucket=self.bucket)
        except _Missing as exc:
            raise RegistryUnavailableError(
                "the SageMaker registry's S3 bucket does not exist", bucket=self.bucket, cause=str(exc)
            ) from exc

    def get_registered_model(self, name: str) -> RegisteredModel:
        tags = self._group(name)["_tags"]
        aliases = {k[len(ALIAS_TAG) :]: v for k, v in tags.items() if k.startswith(ALIAS_TAG)}
        return RegisteredModel(name=name, aliases=aliases)

    def list_versions(self, name: str) -> list[ModelVersion]:
        group = self._group(name)
        summaries: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "ModelPackageGroupName": group["ModelPackageGroupName"],
                "SortBy": "CreationTime",
                "SortOrder": "Ascending",
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            page = self._call(self.sm, "list_model_packages", **kwargs)
            summaries.extend(page.get("ModelPackageSummaryList", []))
            token = page.get("NextToken")
            if not token:
                break
        versions = [
            self._version(
                name,
                self._call(
                    self.sm, "describe_model_package", ModelPackageName=s["ModelPackageArn"]
                ),
            )
            for s in summaries
        ]
        return sorted(versions, key=lambda v: int(v.version))

    def get_version(self, name: str, version: str) -> ModelVersion:
        return self._version(name, self._package(name, version))

    def get_version_metrics(self, name: str, version: str) -> dict[str, float]:
        props = self._package(name, version).get("CustomerMetadataProperties") or {}
        uri = props.get(_METRICS_PROP)
        if uri is None:
            return {}
        try:
            body = self._call(self.s3, "get_object", Bucket=self.bucket, Key=self._s3_key(uri))
        except _Missing as exc:
            raise ArtifactError("the version's metrics file is missing", uri=uri) from exc
        return {k: float(v) for k, v in json.loads(body["Body"].read()).items()}

    def get_version_by_alias(self, name: str, alias: str) -> str:
        version = self.get_registered_model(name).aliases.get(alias)
        if version is None:
            raise ModelNotFoundError(
                f"model '{name}' has no version aliased '{alias}'", model=name, alias=alias
            )
        return version

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self._package(name, version)
        arn = self._group(name)["ModelPackageGroupArn"]
        tag = {"Key": ALIAS_TAG + alias, "Value": version}
        self._call(self.sm, "add_tags", ResourceArn=arn, Tags=[tag])

    def delete_alias(self, name: str, alias: str) -> None:
        group = self._group(name)
        if ALIAS_TAG + alias not in group["_tags"]:
            return
        self._call(
            self.sm,
            "delete_tags",
            ResourceArn=group["ModelPackageGroupArn"],
            TagKeys=[ALIAS_TAG + alias],
        )

    def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
        package = self._package(name, version)
        clean = self._check_properties(tags)
        merged = {**(package.get("CustomerMetadataProperties") or {}), **clean}
        if len(merged) > _PROP_COUNT_MAX:
            raise UnsupportedAdaptationError(
                f"SageMaker keeps at most {_PROP_COUNT_MAX} metadata properties per version",
                model=name,
                version=version,
            )
        self._call(
            self.sm,
            "update_model_package",
            ModelPackageArn=package["ModelPackageArn"],
            CustomerMetadataProperties=clean,
        )

    def download_artifacts(self, name: str, version: str, dst_path: str) -> str:
        package = self._package(name, version)
        containers = (package.get("InferenceSpecification") or {}).get("Containers") or []
        if not containers or "ModelDataUrl" not in containers[0]:
            raise ArtifactError(
                f"Model '{name}' version '{version}' has no model data", model=name
            )
        key = self._s3_key(containers[0]["ModelDataUrl"])
        target = os.path.join(dst_path, "model")
        os.makedirs(target, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sm-download-") as tmp:
            archive = os.path.join(tmp, "model.tar.gz")
            try:
                self._call(
                    self.s3, "download_file", Bucket=self.bucket, Key=key, Filename=archive
                )
                with tarfile.open(archive, "r:gz") as tar:
                    tar.extractall(target, filter="data")
            except (_Missing, tarfile.TarError, OSError) as exc:
                raise ArtifactError(
                    "the model archive could not be extracted", uri=key, cause=str(exc)
                ) from exc
        return target

    def _ensure_group(self, name: str) -> str:
        group = group_name(self.group_prefix, name)
        try:
            return str(self._group(name)["ModelPackageGroupName"])
        except ModelNotFoundError:
            pass
        try:
            described = self._call(
                self.sm, "describe_model_package_group", ModelPackageGroupName=group
            )
        except _Missing:
            self._call(
                self.sm,
                "create_model_package_group",
                ModelPackageGroupName=group,
                ModelPackageGroupDescription=f"oran-adapt model {name}",
                Tags=[{"Key": NAME_TAG, "Value": name}],
            )
            return group
        owner = self._group_tags(described["ModelPackageGroupArn"]).get(NAME_TAG)
        raise ConflictError(
            f"model package group {group!r} already holds model {owner!r}; "
            "rename the model or set SAGEMAKER_GROUP_PREFIX",
            model=name,
            group=group,
            owner=owner,
        )

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
        props = self._check_properties(tags or {})
        if len(props) + 2 > _PROP_COUNT_MAX:
            raise UnsupportedAdaptationError(
                f"SageMaker keeps at most {_PROP_COUNT_MAX} metadata properties per version",
                model=name,
            )
        group = self._ensure_group(name)
        base = f"{self.prefix}/{group}/{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="sm-upload-") as tmp:
            archive = os.path.join(tmp, "model.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                for entry in sorted(os.listdir(artifact_dir)):
                    tar.add(os.path.join(artifact_dir, entry), arcname=entry)
            self._call(
                self.s3,
                "upload_file",
                Filename=archive,
                Bucket=self.bucket,
                Key=f"{base}/model.tar.gz",
            )
        props[_METRICS_PROP] = self._put_json(
            f"{base}/metrics.json", {k: float(v) for k, v in (metrics or {}).items()}
        )
        if input_frame is not None:
            props[_LINEAGE_PROP] = self._put_json(
                f"{base}/lineage.json",
                {
                    "name": input_name,
                    "digest": input_digest,
                    "schema": {str(c): str(t) for c, t in input_frame.dtypes.items()},
                    "rows": len(input_frame),
                },
            )
        created = self._call(
            self.sm,
            "create_model_package",
            ModelPackageGroupName=group,
            ModelPackageDescription=f"oran-adapt {name}",
            InferenceSpecification={
                "Containers": [
                    {
                        "Image": self.inference_image,
                        "ModelDataUrl": self._s3_uri(f"{base}/model.tar.gz"),
                    }
                ],
                "SupportedContentTypes": self.content_types,
                "SupportedResponseMIMETypes": self.content_types,
            },
            ModelApprovalStatus="PendingManualApproval",
            CustomerMetadataProperties=props,
        )
        package = self._call(
            self.sm, "describe_model_package", ModelPackageName=created["ModelPackageArn"]
        )
        return str(package["ModelPackageVersion"])


SPEC = AdapterSpec(
    capability=Capability(
        port="registry",
        adapter="sagemaker",
        description="Amazon SageMaker Model Registry; artifacts as model.tar.gz in S3",
        features=frozenset({"aliases", "version_tags", "version_metrics", "lineage_inputs"}),
        config_keys=(
            "sagemaker_region",
            "sagemaker_s3_bucket",
            "sagemaker_s3_prefix",
            "sagemaker_inference_image",
            "sagemaker_group_prefix",
            "sagemaker_content_types",
            "sagemaker_endpoint_url",
            "registry_tags_checksum",
            "registry_tags_status",
            "artifact_max_bytes",
            "artifact_hash_chunk_bytes",
        ),
        required_keys=("sagemaker_region", "sagemaker_s3_bucket", "sagemaker_inference_image"),
        distributions=("boto3",),
    ),
    factory=SagemakerRegistry.from_settings,
)
