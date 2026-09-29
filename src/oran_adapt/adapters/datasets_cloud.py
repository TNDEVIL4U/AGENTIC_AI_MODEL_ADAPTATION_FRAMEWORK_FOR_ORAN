"""Dataset adapters for cloud object stores: ``s3`` (boto3) and ``gcs`` (the Cloud Storage
JSON API over HTTPS, no SDK).

Both pin what they read. ``stat`` returns the URI of the exact object version that is current
(S3 ``?versionId=`` on a versioned bucket, GCS ``?generation=``), a data version records it,
and later reads open that pinned version, so a registered version keeps reading the same bytes
even after the key is overwritten. On an unversioned S3 bucket the pinned URI is the key
itself and the ETag fingerprint detects a replacement. Only buckets on DATASET_S3_BUCKETS /
DATASET_GCS_BUCKETS are read. Objects are spooled (adapters.datasets.spool) up to
DATASET_MAX_SOURCE_BYTES.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from oran_adapt.adapters.datasets import http_status_error, not_allowed, required, spool
from oran_adapt.core.errors import (
    ConfigurationError,
    DatasetNotFoundError,
    DataSourceUnavailableError,
)
from oran_adapt.ports import AdapterSpec, Capability, SourceStat

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

_CHUNK_BYTES = 1024 * 1024


def _split(uri: str, scheme: str, pin: str) -> tuple[str, str, str | None]:
    """(bucket, key, pinned version or None) of ``<scheme>://bucket/key[?<pin>=...]``."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != scheme:
        raise not_allowed(uri, f"not a {scheme}:// URI")
    key = parts.path.lstrip("/")
    if not parts.netloc or not key:
        raise not_allowed(uri, f"a {scheme} URI needs a bucket and an object key")
    query = parse_qs(parts.query)
    if set(query) - {pin}:
        raise not_allowed(uri, f"the only query parameter accepted is {pin}")
    versions = query.get(pin)
    return parts.netloc, key, versions[0] if versions else None


# ---- S3 -----------------------------------------------------------------------------------
class S3Dataset:
    """``s3://bucket/key`` objects on ``buckets``. ``client`` is a boto3 S3 client (tests use
    a double); ``errors`` are its exception types, ``code_of`` reads an error code from one."""

    schemes = frozenset({"s3"})

    def __init__(
        self,
        buckets: Iterable[str],
        *,
        client: Any,
        errors: tuple[type[BaseException], ...],
        code_of: Callable[[BaseException], str],
        max_bytes: int,
        spool_dir: str | None = None,
    ) -> None:
        self.buckets = set(buckets)
        self.client = client
        self.errors = errors
        self.code_of = code_of
        self.max_bytes = max_bytes
        self.spool_dir = spool_dir

    def _parts(self, uri: str) -> tuple[str, str, str | None]:
        bucket, key, version = _split(uri, "s3", "versionId")
        if bucket not in self.buckets:
            raise not_allowed(uri, "bucket is not in DATASET_S3_BUCKETS", bucket=bucket)
        return bucket, key, version

    def check(self, uri: str) -> None:
        self._parts(uri)

    def _error(self, uri: str, exc: BaseException) -> Exception:
        code = self.code_of(exc)
        if code in ("404", "NoSuchKey", "NoSuchVersion", "NotFound"):
            return DatasetNotFoundError("the referenced S3 object does not exist", uri=uri)
        if code in ("403", "AccessDenied"):
            return not_allowed(uri, "S3 refused access", code=code)
        return DataSourceUnavailableError(f"S3 error: {code or type(exc).__name__}", uri=uri)

    def _args(self, bucket: str, key: str, version: str | None) -> dict[str, str]:
        args = {"Bucket": bucket, "Key": key}
        if version:
            args["VersionId"] = version
        return args

    def stat(self, uri: str) -> SourceStat:
        bucket, key, version = self._parts(uri)
        try:
            head = self.client.head_object(**self._args(bucket, key, version))
        except self.errors as exc:
            raise self._error(uri, exc) from exc
        current = version or head.get("VersionId")
        pinned = f"s3://{bucket}/{key}"
        if current and current != "null":
            pinned += "?" + urlencode({"versionId": current})
        etag = str(head.get("ETag", "")).strip('"') or None
        return SourceStat(uri=uri, pinned_uri=pinned, fingerprint=etag,
                          size_bytes=head.get("ContentLength"))

    def open(self, uri: str) -> BinaryIO:
        bucket, key, version = self._parts(uri)
        try:
            body = self.client.get_object(**self._args(bucket, key, version))["Body"]
            try:
                return spool(iter(lambda: body.read(_CHUNK_BYTES), b""),
                             max_bytes=self.max_bytes, spool_dir=self.spool_dir, uri=uri)
            finally:
                body.close()
        except self.errors as exc:
            raise self._error(uri, exc) from exc


def _s3(settings: Settings) -> S3Dataset:
    buckets = required(settings, "dataset_s3_buckets", "s3")
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise ConfigurationError(
            "DATASET_BACKENDS includes s3, which needs the boto3 package "
            "(pip install 'oran-adapt[aws]')",
            key="DATASET_BACKENDS",
        ) from None
    timeout = settings.dataset_http_timeout_s
    client = boto3.client(
        "s3",
        region_name=settings.dataset_s3_region,
        endpoint_url=settings.dataset_s3_endpoint_url,
        config=Config(connect_timeout=timeout, read_timeout=timeout,
                      retries={"max_attempts": 3}),
    )

    def code_of(exc: BaseException) -> str:
        if isinstance(exc, ClientError):
            return str(exc.response.get("Error", {}).get("Code", ""))
        return ""

    return S3Dataset(buckets, client=client, errors=(BotoCoreError, ClientError),
                     code_of=code_of, max_bytes=settings.dataset_max_source_bytes,
                     spool_dir=settings.dataset_spool_dir)


# ---- GCS ----------------------------------------------------------------------------------
class GcsDataset:
    """``gs://bucket/object`` on ``buckets``, through the JSON API at ``endpoint``. ``token``
    returns a bearer token, or None (an emulator or a public bucket)."""

    schemes = frozenset({"gs"})

    def __init__(
        self,
        buckets: Iterable[str],
        *,
        endpoint: str,
        client: httpx.Client,
        token: Callable[[], str | None],
        max_bytes: int,
        spool_dir: str | None = None,
    ) -> None:
        self.buckets = set(buckets)
        self.endpoint = endpoint.rstrip("/")
        self.client = client
        self.token = token
        self.max_bytes = max_bytes
        self.spool_dir = spool_dir

    def _parts(self, uri: str) -> tuple[str, str, str | None]:
        bucket, key, generation = _split(uri, "gs", "generation")
        if bucket not in self.buckets:
            raise not_allowed(uri, "bucket is not in DATASET_GCS_BUCKETS", bucket=bucket)
        return bucket, key, generation

    def check(self, uri: str) -> None:
        self._parts(uri)

    def _headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _url(self, bucket: str, key: str, *, media: bool) -> str:
        base = f"{self.endpoint}/download" if media else self.endpoint
        return f"{base}/storage/v1/b/{quote(bucket, safe='')}/o/{quote(key, safe='')}"

    def stat(self, uri: str) -> SourceStat:
        bucket, key, generation = self._parts(uri)
        params = {"generation": generation} if generation else {}
        try:
            response = self.client.get(self._url(bucket, key, media=False), params=params,
                                       headers=self._headers())
        except httpx.HTTPError as exc:
            raise DataSourceUnavailableError(f"GCS unreachable: {type(exc).__name__}",
                                             uri=uri) from exc
        if response.status_code != 200:
            raise http_status_error(uri, response.status_code, "gcs")
        meta = response.json()
        current = str(meta.get("generation") or generation or "")
        pinned = f"gs://{bucket}/{key}" + (f"?generation={current}" if current else "")
        mark = meta.get("md5Hash") or meta.get("crc32c") or meta.get("etag")
        size = meta.get("size")
        return SourceStat(uri=uri, pinned_uri=pinned,
                          fingerprint=f"{current}:{mark}" if mark else current or None,
                          size_bytes=int(size) if size is not None else None)

    def open(self, uri: str) -> BinaryIO:
        bucket, key, generation = self._parts(uri)
        params = {"alt": "media", **({"generation": generation} if generation else {})}
        try:
            with self.client.stream("GET", self._url(bucket, key, media=True), params=params,
                                    headers=self._headers()) as response:
                if response.status_code != 200:
                    raise http_status_error(uri, response.status_code, "gcs")
                return spool(response.iter_bytes(_CHUNK_BYTES), max_bytes=self.max_bytes,
                             spool_dir=self.spool_dir, uri=uri)
        except httpx.HTTPError as exc:
            raise DataSourceUnavailableError(f"GCS unreachable: {type(exc).__name__}",
                                             uri=uri) from exc


def _gcs(settings: Settings) -> GcsDataset:
    buckets = required(settings, "dataset_gcs_buckets", "gcs")
    token: Callable[[], str | None]
    if settings.dataset_gcs_credentials == "adc":
        from oran_adapt.adapters.registry.vertex import AdcToken

        token = AdcToken()
    else:
        def token() -> str | None:
            return None
    return GcsDataset(
        buckets,
        endpoint=settings.dataset_gcs_endpoint,
        client=httpx.Client(timeout=settings.dataset_http_timeout_s, follow_redirects=False),
        token=token,
        max_bytes=settings.dataset_max_source_bytes,
        spool_dir=settings.dataset_spool_dir,
    )


S3 = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="s3",
        description="s3:// objects on DATASET_S3_BUCKETS, pinned to their version id",
        features=frozenset({"network", "fingerprint", "pinned"}),
        config_keys=("dataset_s3_buckets", "dataset_s3_region", "dataset_s3_endpoint_url",
                     "dataset_http_timeout_s", "dataset_max_source_bytes",
                     "dataset_spool_dir"),
        required_keys=("dataset_s3_buckets",),
        distributions=("boto3",),
    ),
    factory=_s3,
)

GCS = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="gcs",
        description="gs:// objects on DATASET_GCS_BUCKETS, pinned to their generation",
        features=frozenset({"network", "fingerprint", "pinned"}),
        config_keys=("dataset_gcs_buckets", "dataset_gcs_endpoint", "dataset_gcs_credentials",
                     "dataset_http_timeout_s", "dataset_max_source_bytes",
                     "dataset_spool_dir"),
        required_keys=("dataset_gcs_buckets",),
    ),
    factory=_gcs,
)
