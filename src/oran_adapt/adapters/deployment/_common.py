"""Helpers the deployment adapters share: templated names and URIs, bearer tokens, and an HTTP
client wrapper that maps transport failures to DeploymentUnavailableError."""

from __future__ import annotations

import hashlib
import re
import string
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from oran_adapt.core.errors import ConfigurationError, DeploymentUnavailableError
from oran_adapt.core.model_uri import check_model_name
from oran_adapt.ports import DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from pydantic import SecretStr

# Placeholders a name, path or URI template may use.
TEMPLATE_FIELDS = frozenset({"model", "name", "version", "source"})
_DNS_MAX = 63


def dns_name(prefix: str, model: str) -> str:
    """A DNS-1123 label (Kubernetes object names, endpoint ids) for ``model``: lower-cased,
    other characters turned into ``-``, and a hash suffix when that changed the name or it is
    too long, so two model names never map to one object."""
    raw = prefix + check_model_name(model)
    base = re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-") or "m"
    if not base[0].isalpha():
        base = f"m{base}"
    if base == raw and len(base) <= _DNS_MAX:
        return base
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return f"{base[: _DNS_MAX - len(digest) - 1].rstrip('-')}-{digest}"


def check_template(key: str, template: str, allowed: frozenset[str] = TEMPLATE_FIELDS) -> str:
    """``template`` if it uses only ``allowed`` placeholders; ConfigurationError naming ``key``."""
    try:
        used = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    except ValueError as exc:
        raise ConfigurationError(f"{key.upper()} is not a valid template: {exc}", key=key) from exc
    unknown = used - allowed
    if unknown:
        raise ConfigurationError(
            f"{key.upper()} uses unknown placeholders {sorted(unknown)}; "
            f"allowed: {sorted(allowed)}",
            key=key,
        )
    return template


def render(template: str, target: DeploymentTarget, name: str) -> str:
    return template.format(
        model=target.model, name=name, version=target.version, source=target.source or ""
    )


def required(value: str | None, key: str) -> str:
    """A required key's value (config validation already enforced it when the adapter was
    selected; this guards direct construction)."""
    if not value:
        raise ConfigurationError(f"{key.upper()} must be set for this deployment adapter", key=key)
    return value


class StaticToken:
    def __init__(self, token: SecretStr | None) -> None:
        self._token = token

    def __call__(self) -> str | None:
        return self._token.get_secret_value() if self._token is not None else None


class FileToken:
    """Read on every call: projected service-account tokens are rotated in place."""

    def __init__(self, path: str) -> None:
        self.path = path

    def __call__(self) -> str | None:
        try:
            return Path(self.path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DeploymentUnavailableError(
                "the deployment token file cannot be read", path=self.path, cause=str(exc)
            ) from exc


class HttpApi:
    """One serving system's HTTP API. ``call`` returns the response for any status below 500
    except 401/403; transport failures, 5xx and auth failures raise DeploymentUnavailableError.
    The client is built lazily from ``http_factory`` and dropped on pickling."""

    def __init__(
        self,
        base_url: str,
        *,
        service: str,
        http_factory: Callable[[], httpx.Client],
        token: Callable[[], str | None],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.http_factory = http_factory
        self.token = token
        self._client: httpx.Client | None = None

    def __getstate__(self) -> dict[str, Any]:
        return {**self.__dict__, "_client": None}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self.http_factory()
        return self._client

    def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}))
        token = self.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise DeploymentUnavailableError(
                f"{self.service} is not reachable", url=url, cause=str(exc)
            ) from exc
        if response.status_code >= 500 or response.status_code in (401, 403):
            raise DeploymentUnavailableError(
                f"{self.service} request failed with HTTP {response.status_code}",
                url=url,
                cause=response.text[:500],
            )
        return response


def parse_status(model: str, body: Any, service: str) -> DeploymentState:
    """A status document of the webhook contract: {"version": str|null, "ready": bool,
    "failed": bool (optional), "detail": str (optional)}."""
    if not isinstance(body, dict) or "version" not in body or "ready" not in body:
        raise DeploymentUnavailableError(
            f"{service} returned a status document without 'version' and 'ready'",
            body=str(body)[:500],
        )
    version = body["version"]
    return DeploymentState(
        model=model,
        version=None if version is None else str(version),
        ready=bool(body["ready"]),
        failed=bool(body.get("failed", False)),
        detail=str(body.get("detail", "")),
    )
