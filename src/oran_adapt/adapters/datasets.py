"""Dataset adapters (port ``dataset``) that read data objects by reference: ``file``, ``http``
and ``fsspec``. The cloud object stores (``s3``, ``gcs``) are in adapters.datasets_cloud.

Each adapter only reads, and only inside its allow-list: DATASET_FILE_ROOTS (directories),
DATASET_HTTP_ALLOWED_HOSTS (host names, HTTPS unless DATASET_HTTP_ALLOW_PLAIN) or
DATASET_FSSPEC_PREFIXES (URI prefixes). An empty allow-list is a startup ConfigurationError,
never "allow everything". ``check`` decides from the URI alone, before any I/O.

``stat`` returns a fingerprint that changes when the object's content changes (file: size and
modification time; HTTP: ETag, else Last-Modified and length; fsspec: the filesystem's ETag,
checksum or modification time), which is how a registered data version notices that its object
was replaced. Remote objects are spooled to a temporary file (DATASET_SPOOL_DIR) so readers get
a seekable handle, and the spool stops at DATASET_MAX_SOURCE_BYTES.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from urllib.parse import urlsplit
from urllib.request import url2pathname

import httpx

from oran_adapt.core.errors import (
    ConfigurationError,
    DatasetNotFoundError,
    DataSourceNotAllowedError,
    DataSourceUnavailableError,
    DataTooLargeError,
)
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability, SourceStat

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

_CHUNK_BYTES = 1024 * 1024


def required(settings: Settings, key: str, adapter: str) -> Any:
    value = getattr(settings, key)
    if value is None or value == "" or value == []:
        raise ConfigurationError(
            f"DATASET_BACKENDS includes {adapter}, which requires {key.upper()}",
            key=key.upper(),
        )
    return value


def spool(
    chunks: Iterable[bytes], *, max_bytes: int, spool_dir: str | None, uri: str
) -> BinaryIO:
    """``chunks`` written to an anonymous temporary file (removed when closed), rewound.
    Stops with DataTooLargeError past ``max_bytes``."""
    fh = tempfile.TemporaryFile(dir=spool_dir)  # noqa: SIM115 - the caller closes it
    written = 0
    try:
        for chunk in chunks:
            written += len(chunk)
            if written > max_bytes:
                raise DataTooLargeError(
                    "the referenced object is larger than DATASET_MAX_SOURCE_BYTES",
                    key="DATASET_MAX_SOURCE_BYTES",
                    limit=max_bytes,
                    uri=uri,
                )
            fh.write(chunk)
        fh.seek(0)
    except BaseException:
        fh.close()
        raise
    return cast("BinaryIO", fh)


def not_allowed(uri: str, why: str, **context: object) -> DataSourceNotAllowedError:
    return DataSourceNotAllowedError(f"data source not allowed: {why}", uri=uri, **context)


def http_status_error(uri: str, status: int, adapter: str) -> Exception:
    """What an HTTP status from a data store means for the caller."""
    if status == 404:
        return DatasetNotFoundError("the referenced data object does not exist", uri=uri,
                                    adapter=adapter)
    if status in (401, 403):
        return not_allowed(uri, f"the {adapter} server refused access (HTTP {status})",
                           status_code=status)
    if 300 <= status < 400:
        return not_allowed(uri, f"the {adapter} server redirected (HTTP {status}); "
                           "redirects are not followed", status_code=status)
    return DataSourceUnavailableError(f"the {adapter} server answered HTTP {status}", uri=uri,
                                      status_code=status)


# ---- file ---------------------------------------------------------------------------------
class FileDataset:
    """``file://`` URIs under one of ``roots``. Paths are resolved (``..`` and symlinks
    followed) before the containment check, so neither escapes a root."""

    schemes = frozenset({"file"})

    def __init__(self, roots: Iterable[str]) -> None:
        self.roots = [Path(r).resolve() for r in roots]

    def _path(self, uri: str) -> Path:
        parts = urlsplit(uri)
        if parts.scheme.lower() != "file":
            raise not_allowed(uri, "not a file:// URI")
        if parts.netloc not in ("", "localhost"):
            raise not_allowed(uri, "file URIs on another host are not read", host=parts.netloc)
        if parts.query or parts.fragment:
            raise not_allowed(uri, "file URIs take no query or fragment")
        path = Path(url2pathname(parts.path)).resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.roots):
            raise not_allowed(uri, "path is outside DATASET_FILE_ROOTS")
        return path

    def check(self, uri: str) -> None:
        self._path(uri)

    def stat(self, uri: str) -> SourceStat:
        path = self._path(uri)
        try:
            st = path.stat()
        except FileNotFoundError:
            raise DatasetNotFoundError("the referenced data file does not exist",
                                       uri=uri) from None
        except OSError as exc:
            raise DataSourceUnavailableError(f"cannot read the data file: {exc.strerror}",
                                             uri=uri) from exc
        if not path.is_file():
            raise DatasetNotFoundError("the referenced path is not a file", uri=uri)
        return SourceStat(uri=uri, pinned_uri=uri, fingerprint=f"{st.st_size}-{st.st_mtime_ns}",
                          size_bytes=st.st_size)

    def open(self, uri: str) -> BinaryIO:
        path = self._path(uri)
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise DatasetNotFoundError("the referenced data file does not exist",
                                       uri=uri) from None
        except OSError as exc:
            raise DataSourceUnavailableError(f"cannot read the data file: {exc.strerror}",
                                             uri=uri) from exc


def _file(settings: Settings) -> FileDataset:
    return FileDataset(required(settings, "dataset_file_roots", "file"))


# ---- http ---------------------------------------------------------------------------------
class HttpDataset:
    """HTTPS URLs on ``allowed_hosts`` (plain HTTP too when ``allow_plain``). Redirects are
    not followed, so a URL cannot bounce the read to a host off the list."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        allow_plain: bool = False,
        token: str | None = None,
        client: httpx.Client,
        max_bytes: int,
        spool_dir: str | None = None,
    ) -> None:
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.schemes = frozenset({"https", "http"} if allow_plain else {"https"})
        self.token = token
        self.client = client
        self.max_bytes = max_bytes
        self.spool_dir = spool_dir

    def check(self, uri: str) -> None:
        parts = urlsplit(uri)
        if parts.scheme.lower() not in self.schemes:
            raise not_allowed(uri, "only https URLs are read (see DATASET_HTTP_ALLOW_PLAIN)")
        if parts.username or parts.password:
            raise not_allowed(uri, "credentials in the URL are not accepted "
                              "(use DATASET_HTTP_TOKEN)")
        if (parts.hostname or "").lower() not in self.allowed_hosts:
            raise not_allowed(uri, "host is not in DATASET_HTTP_ALLOWED_HOSTS",
                              host=parts.hostname)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def stat(self, uri: str) -> SourceStat:
        self.check(uri)
        try:
            response = self.client.head(uri, headers=self._headers())
            if response.status_code == 405:  # no HEAD support: read the headers of a GET
                with self.client.stream("GET", uri, headers=self._headers()) as streamed:
                    response = streamed
        except httpx.HTTPError as exc:
            raise DataSourceUnavailableError(f"data server unreachable: {type(exc).__name__}",
                                             uri=uri) from exc
        if response.status_code != 200:
            raise http_status_error(uri, response.status_code, "http")
        etag = response.headers.get("etag")
        modified = response.headers.get("last-modified")
        length = response.headers.get("content-length")
        fingerprint = etag or (f"{modified}|{length}" if modified else None)
        return SourceStat(uri=uri, pinned_uri=uri, fingerprint=fingerprint,
                          size_bytes=int(length) if length and length.isdigit() else None)

    def open(self, uri: str) -> BinaryIO:
        self.check(uri)
        try:
            with self.client.stream("GET", uri, headers=self._headers()) as response:
                if response.status_code != 200:
                    raise http_status_error(uri, response.status_code, "http")
                return spool(response.iter_bytes(_CHUNK_BYTES), max_bytes=self.max_bytes,
                             spool_dir=self.spool_dir, uri=uri)
        except httpx.HTTPError as exc:
            raise DataSourceUnavailableError(f"data server unreachable: {type(exc).__name__}",
                                             uri=uri) from exc


def _http(settings: Settings) -> HttpDataset:
    token = settings.dataset_http_token
    return HttpDataset(
        required(settings, "dataset_http_allowed_hosts", "http"),
        allow_plain=settings.dataset_http_allow_plain,
        token=token.get_secret_value() if token is not None else None,
        client=OutboundPolicy.from_settings(settings).client(
            timeout=settings.dataset_http_timeout_s
        ),
        max_bytes=settings.dataset_max_source_bytes,
        spool_dir=settings.dataset_spool_dir,
    )


# ---- fsspec -------------------------------------------------------------------------------
_FINGERPRINT_FIELDS = ("ETag", "etag", "md5Hash", "md5", "checksum", "generation",
                       "VersionId", "LastModified", "last_modified", "mtime", "updated",
                       "created")


class FsspecDataset:
    """Any fsspec filesystem (``memory``, ``sftp``, ``abfs``, ``hf`` ...), for URIs under
    ``prefixes``. ``filesystem(protocol)`` returns the filesystem for a protocol."""

    def __init__(self, prefixes: Iterable[str],
                 filesystem: Callable[[str], Any]) -> None:
        self.prefixes = [p.rstrip("/") + "/" for p in prefixes]
        self.schemes = frozenset(urlsplit(p).scheme.lower() for p in self.prefixes)
        self._filesystem = filesystem

    def check(self, uri: str) -> None:
        if ".." in uri.split("/"):
            raise not_allowed(uri, "path segments '..' are not accepted")
        if not any(uri.startswith(p) for p in self.prefixes):
            raise not_allowed(uri, "URI is outside DATASET_FSSPEC_PREFIXES")

    def _fs(self, uri: str) -> tuple[Any, str]:
        self.check(uri)
        protocol = urlsplit(uri).scheme.lower()
        return self._filesystem(protocol), uri

    def stat(self, uri: str) -> SourceStat:
        fs, path = self._fs(uri)
        try:
            info = fs.info(path)
        except FileNotFoundError:
            raise DatasetNotFoundError("the referenced data object does not exist",
                                       uri=uri) from None
        except OSError as exc:
            raise DataSourceUnavailableError(f"filesystem error: {type(exc).__name__}",
                                             uri=uri) from exc
        if info.get("type") == "directory":
            raise DatasetNotFoundError("the referenced path is a directory", uri=uri)
        size = info.get("size")
        marks = [f"{k}={info[k]}" for k in _FINGERPRINT_FIELDS if info.get(k) is not None]
        fingerprint = "|".join([f"size={size}", *marks]) if marks else None
        return SourceStat(uri=uri, pinned_uri=uri, fingerprint=fingerprint,
                          size_bytes=int(size) if size is not None else None)

    def open(self, uri: str) -> BinaryIO:
        fs, path = self._fs(uri)
        try:
            return cast("BinaryIO", fs.open(path, "rb"))
        except FileNotFoundError:
            raise DatasetNotFoundError("the referenced data object does not exist",
                                       uri=uri) from None
        except OSError as exc:
            raise DataSourceUnavailableError(f"filesystem error: {type(exc).__name__}",
                                             uri=uri) from exc


def fsspec_options(settings: Settings) -> Mapping[str, dict]:
    """DATASET_FSSPEC_OPTIONS: a JSON object of per-protocol storage options."""
    raw = settings.dataset_fsspec_options
    if raw is None:
        return {}
    try:
        options = json.loads(raw.get_secret_value())
    except ValueError:
        raise ConfigurationError("DATASET_FSSPEC_OPTIONS is not valid JSON",
                                 key="DATASET_FSSPEC_OPTIONS") from None
    if not isinstance(options, dict) or not all(isinstance(v, dict) for v in options.values()):
        raise ConfigurationError(
            'DATASET_FSSPEC_OPTIONS must be {"<protocol>": {<storage options>}, ...}',
            key="DATASET_FSSPEC_OPTIONS",
        )
    return options


def _fsspec(settings: Settings) -> FsspecDataset:
    prefixes = required(settings, "dataset_fsspec_prefixes", "fsspec")
    options = fsspec_options(settings)
    try:
        import fsspec
    except ImportError:
        raise ConfigurationError(
            "DATASET_BACKENDS includes fsspec, which needs the fsspec package "
            "(pip install 'oran-adapt[fsspec]')",
            key="DATASET_BACKENDS",
        ) from None

    def filesystem(protocol: str) -> Any:
        return fsspec.filesystem(protocol, **options.get(protocol, {}))

    return FsspecDataset(prefixes, filesystem)


FILE = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="file",
        description="file:// objects under DATASET_FILE_ROOTS",
        features=frozenset({"local", "fingerprint"}),
        config_keys=("dataset_file_roots",),
        required_keys=("dataset_file_roots",),
    ),
    factory=_file,
)

HTTP = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="http",
        description="https:// objects on DATASET_HTTP_ALLOWED_HOSTS (spooled, no redirects)",
        features=frozenset({"network", "fingerprint"}),
        config_keys=("dataset_http_allowed_hosts", "dataset_http_allow_plain",
                     "dataset_http_timeout_s", "dataset_http_token",
                     "dataset_max_source_bytes", "dataset_spool_dir"),
        required_keys=("dataset_http_allowed_hosts",),
    ),
    factory=_http,
)

FSSPEC = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="fsspec",
        description="objects on any fsspec filesystem under DATASET_FSSPEC_PREFIXES",
        features=frozenset({"network", "fingerprint"}),
        config_keys=("dataset_fsspec_prefixes", "dataset_fsspec_options"),
        required_keys=("dataset_fsspec_prefixes",),
        distributions=("fsspec",),
    ),
    factory=_fsspec,
)

__all__ = ["FILE", "FSSPEC", "HTTP", "FileDataset", "FsspecDataset", "HttpDataset", "spool"]
