"""In-memory emulators of the cloud APIs the SageMaker and Vertex registry adapters call.

They reproduce the calls, response shapes, pagination and error codes each adapter depends
on, as documented by AWS (boto3 ``sagemaker`` / ``s3`` clients) and Google (Vertex AI v1 REST,
GCS JSON API). They are emulators, not the services: conformance against them is
"unverified against AWS/GCP"; the live runs are the heavy tests in test_phase2_registry.py.

Page sizes are deliberately tiny so every paginated path in the adapters is exercised.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx

PAGE = 2
ACCOUNT = "000000000000"
REGION = "eu-west-1"


# ---- AWS -------------------------------------------------------------------------------


class ClientError(Exception):
    """Shaped like botocore.exceptions.ClientError: the code lives in ``.response``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def _page(items: list[Any], token: str | None, size: int = PAGE) -> tuple[list[Any], str | None]:
    start = int(token or 0)
    end = start + size
    return items[start:end], (str(end) if end < len(items) else None)


class SagemakerEmulator:
    """The ``sagemaker`` client calls SagemakerRegistry makes."""

    def __init__(self) -> None:
        self.groups: dict[str, dict[str, Any]] = {}  # name -> {arn, tags, packages}
        self.fail_next: str | None = None  # method name to fail once with ThrottlingException

    def _maybe_fail(self, method: str) -> None:
        if self.fail_next == method:
            self.fail_next = None
            raise ClientError("ThrottlingException", "Rate exceeded")

    def _group_arn(self, name: str) -> str:
        return f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:model-package-group/{name}"

    def _by_arn(self, arn: str) -> dict[str, Any]:
        for group in self.groups.values():
            if group["arn"] == arn:
                return group
        raise ClientError("ValidationException", f"Resource {arn} does not exist.")

    def _group(self, name: str) -> dict[str, Any]:
        if name not in self.groups:
            raise ClientError("ValidationException", f"ModelPackageGroup {name} does not exist.")
        return self.groups[name]

    def list_model_package_groups(self, MaxResults: int) -> dict[str, Any]:
        self._maybe_fail("list_model_package_groups")
        return {"ModelPackageGroupSummaryList": []}

    def describe_model_package_group(self, ModelPackageGroupName: str) -> dict[str, Any]:
        self._maybe_fail("describe_model_package_group")
        group = self._group(ModelPackageGroupName)
        return {"ModelPackageGroupName": ModelPackageGroupName, "ModelPackageGroupArn": group["arn"]}

    def create_model_package_group(self, **kw: Any) -> dict[str, Any]:
        name = kw["ModelPackageGroupName"]
        if name in self.groups:
            raise ClientError("ValidationException", f"Model Package Group {name} already exists")
        tags = {t["Key"]: t["Value"] for t in kw.get("Tags", [])}
        self.groups[name] = {"arn": self._group_arn(name), "tags": tags, "packages": []}
        return {"ModelPackageGroupArn": self._group_arn(name)}

    def list_tags(self, ResourceArn: str, NextToken: str | None = None) -> dict[str, Any]:
        tags = sorted(self._by_arn(ResourceArn)["tags"].items())
        items, token = _page([{"Key": k, "Value": v} for k, v in tags], NextToken)
        return {"Tags": items, **({"NextToken": token} if token else {})}

    def add_tags(self, ResourceArn: str, Tags: list[dict[str, str]]) -> dict[str, Any]:
        self._by_arn(ResourceArn)["tags"].update({t["Key"]: t["Value"] for t in Tags})
        return {"Tags": Tags}

    def delete_tags(self, ResourceArn: str, TagKeys: list[str]) -> dict[str, Any]:
        tags = self._by_arn(ResourceArn)["tags"]
        for key in TagKeys:
            tags.pop(key, None)
        return {}

    def create_model_package(self, **kw: Any) -> dict[str, Any]:
        self._maybe_fail("create_model_package")
        group = self._group(kw["ModelPackageGroupName"])
        version = len(group["packages"]) + 1
        arn = f"{group['arn'].replace(':model-package-group/', ':model-package/')}/{version}"
        group["packages"].append(
            {
                "ModelPackageGroupName": kw["ModelPackageGroupName"],
                "ModelPackageVersion": version,
                "ModelPackageArn": arn,
                "ModelPackageStatus": "Completed",
                "ModelApprovalStatus": kw["ModelApprovalStatus"],
                "InferenceSpecification": kw["InferenceSpecification"],
                "CustomerMetadataProperties": dict(kw.get("CustomerMetadataProperties", {})),
                "CreationTime": datetime.now(UTC),
            }
        )
        return {"ModelPackageArn": arn}

    def _package(self, arn: str) -> dict[str, Any]:
        for group in self.groups.values():
            for package in group["packages"]:
                if package["ModelPackageArn"] == arn:
                    return package
        raise ClientError("ValidationException", f"Model package {arn} does not exist.")

    def describe_model_package(self, ModelPackageName: str) -> dict[str, Any]:
        return copy.deepcopy(self._package(ModelPackageName))

    def list_model_packages(self, **kw: Any) -> dict[str, Any]:
        group = self._group(kw["ModelPackageGroupName"])
        packages = sorted(group["packages"], key=lambda p: p["CreationTime"])
        if kw.get("SortOrder") == "Descending":
            packages.reverse()
        summaries = [
            {"ModelPackageArn": p["ModelPackageArn"], "ModelPackageVersion": p["ModelPackageVersion"]}
            for p in packages
        ]
        items, token = _page(summaries, kw.get("NextToken"), min(PAGE, kw.get("MaxResults", PAGE)))
        return {"ModelPackageSummaryList": items, **({"NextToken": token} if token else {})}

    def update_model_package(self, **kw: Any) -> dict[str, Any]:
        # UpdateModelPackage merges CustomerMetadataProperties into the existing ones.
        package = self._package(kw["ModelPackageArn"])
        package["CustomerMetadataProperties"].update(kw.get("CustomerMetadataProperties", {}))
        for key in kw.get("CustomerMetadataPropertiesToRemove", []):
            package["CustomerMetadataProperties"].pop(key, None)
        return {"ModelPackageArn": kw["ModelPackageArn"]}


class S3Emulator:
    """The ``s3`` client calls SagemakerRegistry makes."""

    def __init__(self, *buckets: str) -> None:
        self.objects: dict[str, dict[str, bytes]] = {b: {} for b in buckets}

    def _bucket(self, bucket: str) -> dict[str, bytes]:
        if bucket not in self.objects:
            raise ClientError("NoSuchBucket", f"The specified bucket {bucket} does not exist")
        return self.objects[bucket]

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.objects:
            raise ClientError("404", "Not Found")
        return {}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:
        self._bucket(Bucket)[Key] = bytes(Body)
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        objects = self._bucket(Bucket)
        if Key not in objects:
            raise ClientError("NoSuchKey", "The specified key does not exist.")
        return {"Body": io.BytesIO(objects[Key])}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        with open(Filename, "rb") as fh:
            self._bucket(Bucket)[Key] = fh.read()

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        body = self.get_object(Bucket=Bucket, Key=Key)["Body"]
        with open(Filename, "wb") as fh:
            shutil.copyfileobj(body, fh)


# ---- Google ----------------------------------------------------------------------------

API_HOST = "vertex.emulator"
STORAGE_HOST = "gcs.emulator"
TOKEN = "emulator-token"


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


class VertexEmulator:
    """Vertex AI v1 model registry + GCS JSON API, behind an httpx.MockTransport."""

    def __init__(self, project: str, location: str, bucket: str) -> None:
        self.parent = f"projects/{project}/locations/{location}"
        self.bucket = bucket
        self.models: dict[str, dict[str, Any]] = {}  # id -> {displayName, versions: [...]}
        self.objects: dict[str, tuple[bytes, int]] = {}  # name -> (data, generation)
        self.operations: dict[str, dict[str, Any]] = {}
        self.generation = 0
        self.requests = 0

    # -- dispatch --

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _json(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        if request.url.host == STORAGE_HOST:
            return self._storage(request)
        if request.url.host == API_HOST:
            return self._vertex(request)
        return _json(404, {"error": {"code": 404}})

    # -- GCS --

    def _storage(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.raw_path.decode().split("?", 1)[0])
        params = request.url.params
        upload = f"/upload/storage/v1/b/{self.bucket}/o"
        objects = f"/storage/v1/b/{self.bucket}/o"
        if request.method == "POST" and path == upload:
            name = params["name"]
            current = self.objects.get(name)
            match = params.get("ifGenerationMatch")
            if match is not None and str(current[1] if current else 0) != match:
                return _json(412, {"error": {"code": 412, "message": "conditionNotMet"}})
            self.generation += 1
            self.objects[name] = (request.content, self.generation)
            return _json(200, {"name": name, "generation": str(self.generation)})
        if request.method == "GET" and path == f"/storage/v1/b/{self.bucket}":
            return _json(200, {"name": self.bucket})
        if request.method == "GET" and path == objects:
            names = sorted(n for n in self.objects if n.startswith(params.get("prefix", "")))
            items, token = _page([{"name": n} for n in names], params.get("pageToken"))
            return _json(200, {"items": items, **({"nextPageToken": token} if token else {})})
        if request.method == "GET" and path.startswith(objects + "/"):
            name = path[len(objects) + 1 :]
            if name not in self.objects or params.get("alt") != "media":
                return _json(404, {"error": {"code": 404}})
            data, generation = self.objects[name]
            return httpx.Response(200, content=data, headers={"x-goog-generation": str(generation)})
        return _json(404, {"error": {"code": 404}})

    # -- Vertex --

    def _view(self, mid: str, version: dict[str, Any]) -> dict[str, Any]:
        model = self.models[mid]
        return {
            "name": f"{self.parent}/models/{mid}",
            "displayName": model["displayName"],
            "versionId": str(version["versionId"]),
            "versionAliases": list(version["versionAliases"]),
            "artifactUri": version["artifactUri"],
            "containerSpec": version["containerSpec"],
            "createTime": model["createTime"],
            "versionCreateTime": version["versionCreateTime"],
        }

    def _vertex(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/v1/{self.parent}/models"
        if path.startswith(f"/v1/{self.parent}/operations/"):
            return self._operation(path[len("/v1/") :])
        if path == prefix and request.method == "GET":
            return _json(200, {"models": []})
        if path == prefix + ":upload" and request.method == "POST":
            return self._upload(json.loads(request.content))
        if not path.startswith(prefix + "/"):
            return _json(404, {"error": {"code": 404}})
        ref, _, verb = path[len(prefix) + 1 :].partition(":")
        mid, _, version_id = ref.partition("@")
        model = self.models.get(mid)
        if model is None:
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if verb == "listVersions" and request.method == "GET":
            views = [self._view(mid, v) for v in model["versions"]]
            items, token = _page(views, request.url.params.get("pageToken"))
            return _json(200, {"models": items, **({"nextPageToken": token} if token else {})})
        if not version_id and not verb:
            default = [v for v in model["versions"] if "default" in v["versionAliases"]]
            if not default:
                return _json(200, {"name": f"{self.parent}/models/{mid}",
                                   "displayName": model["displayName"]})
            return _json(200, self._view(mid, default[0]))
        version = next((v for v in model["versions"] if str(v["versionId"]) == version_id), None)
        if version is None:
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if not verb and request.method == "GET":
            return _json(200, self._view(mid, version))
        if verb == "mergeVersionAliases" and request.method == "POST":
            return self._merge(mid, version, json.loads(request.content)["versionAliases"])
        return _json(404, {"error": {"code": 404}})

    def _merge(self, mid: str, version: dict[str, Any], aliases: list[str]) -> httpx.Response:
        for alias in aliases:
            if alias.startswith("-"):
                if alias[1:] == "default":
                    return _json(400, {"error": {"code": 400, "message": "default is reserved"}})
                if alias[1:] in version["versionAliases"]:
                    version["versionAliases"].remove(alias[1:])
                continue
            taken = [
                v for v in self.models[mid]["versions"]
                if v is not version and alias in v["versionAliases"]
            ]
            if taken:
                message = f"alias {alias} is already used by another version"
                return _json(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                             "message": message}})
            if alias not in version["versionAliases"]:
                version["versionAliases"].append(alias)
        return _json(200, self._view(mid, version))

    def _upload(self, body: dict[str, Any]) -> httpx.Response:
        spec = body["model"]
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if "parentModel" in body:
            mid = body["parentModel"].rsplit("/", 1)[1]
            if mid not in self.models:
                return _json(404, {"error": {"code": 404}})
        else:
            mid = body["modelId"]
            if mid in self.models:
                return _json(409, {"error": {"code": 409, "status": "ALREADY_EXISTS"}})
            self.models[mid] = {"displayName": spec["displayName"], "createTime": now,
                                "versions": []}
        versions = self.models[mid]["versions"]
        version = {
            "versionId": len(versions) + 1,
            "versionAliases": [] if versions else ["default"],
            "artifactUri": spec["artifactUri"],
            "containerSpec": spec["containerSpec"],
            "versionCreateTime": now,
        }
        versions.append(version)
        name = f"{self.parent}/operations/{len(self.operations) + 1}"
        # The first poll reports the operation still running, so the adapter's wait loop runs.
        self.operations[name] = {"polls": 0, "response": {
            "model": f"{self.parent}/models/{mid}", "modelVersionId": str(version["versionId"])}}
        return _json(200, {"name": name, "done": False})

    def _operation(self, name: str) -> httpx.Response:
        operation = self.operations.get(name)
        if operation is None:
            return _json(404, {"error": {"code": 404}})
        operation["polls"] += 1
        if operation["polls"] < 2:
            return _json(200, {"name": name, "done": False})
        return _json(200, {"name": name, "done": True, "response": operation["response"]})


class EmulatorClientFactory:
    """A picklable ``http_factory`` returning an httpx client wired to the emulator."""

    def __init__(self, emulator: VertexEmulator) -> None:
        self.emulator = emulator

    def __call__(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.emulator.handle))


def emulator_token() -> str:
    return TOKEN
