"""Secrets adapter ``vault``: HashiCorp Vault (or OpenBao) KV version 2, over its HTTP API.

One secret document at ``<SECRETS_VAULT_MOUNT>/data/<SECRETS_VAULT_PATH>`` holds every secret
the framework reads, one field per setting name in lower case (``anthropic_api_key``,
``dataset_http_token``, ...). It is read once, when the configuration loads, so rotation takes a
restart (like the ``file`` backend).

The Vault token is read from SECRETS_VAULT_TOKEN_FILE (a Vault Agent sink or a mounted secret):
never from the configuration itself, which is what the token protects. SECRETS_VAULT_NAMESPACE
is sent as ``X-Vault-Namespace`` (Vault Enterprise / HCP). The request goes through the outbound
policy like every other call (SSRF check, TLS verification, OUTBOUND_CA_FILE for a private CA).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from oran_adapt.core.errors import ConfigurationError
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


class VaultSecrets:
    def __init__(
        self,
        url: str,
        *,
        mount: str,
        path: str,
        token: str,
        namespace: str | None,
        policy: OutboundPolicy,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.location = f"{url.rstrip('/')}/v1/{mount.strip('/')}/data/{path.strip('/')}"
        self._headers = {"X-Vault-Token": token}
        if namespace:
            self._headers["X-Vault-Namespace"] = namespace
        self._client = policy.client(timeout=timeout_s, transport=transport)
        self._data: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        try:
            response = self._client.get(self.location, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ConfigurationError("Vault could not be reached", key="SECRETS_VAULT_URL",
                                     cause=type(exc).__name__) from exc
        if response.status_code == 404:
            return {}
        if response.status_code in (401, 403):
            raise ConfigurationError("Vault refused the token", key="SECRETS_VAULT_TOKEN_FILE",
                                     status=response.status_code)
        if response.status_code >= 400:
            raise ConfigurationError(f"Vault answered HTTP {response.status_code}",
                                     key="SECRETS_VAULT_URL")
        try:
            data = response.json()["data"]["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigurationError("Vault's answer is not a KV v2 secret",
                                     key="SECRETS_VAULT_PATH") from exc
        return dict(data) if isinstance(data, dict) else {}

    def get(self, name: str) -> str | None:
        if self._data is None:
            self._data = self._read()
        value = self._data.get(name.lower())
        return None if value is None else str(value)


def _token(path: str | None) -> str:
    if not path:
        raise ConfigurationError("SECRETS_BACKEND=vault requires SECRETS_VAULT_TOKEN_FILE",
                                 key="SECRETS_VAULT_TOKEN_FILE")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError("SECRETS_VAULT_TOKEN_FILE cannot be read",
                                 key="SECRETS_VAULT_TOKEN_FILE", path=path) from exc
    if not token:
        raise ConfigurationError("SECRETS_VAULT_TOKEN_FILE is empty",
                                 key="SECRETS_VAULT_TOKEN_FILE", path=path)
    return token


def _vault(settings: Settings) -> VaultSecrets:
    url = settings.secrets_vault_url
    if not url:
        raise ConfigurationError("SECRETS_BACKEND=vault requires SECRETS_VAULT_URL",
                                 key="SECRETS_VAULT_URL")
    return VaultSecrets(
        url,
        mount=settings.secrets_vault_mount,
        path=settings.secrets_vault_path,
        token=_token(settings.secrets_vault_token_file),
        namespace=settings.secrets_vault_namespace,
        policy=OutboundPolicy.from_settings(settings),
        timeout_s=settings.secrets_vault_timeout_s,
    )


VAULT = AdapterSpec(
    capability=Capability(
        port="secrets",
        adapter="vault",
        description="HashiCorp Vault / OpenBao KV v2, one document per deployment",
        features=frozenset({"read", "rotation_on_restart", "central_store"}),
        config_keys=("secrets_vault_url", "secrets_vault_mount", "secrets_vault_path",
                     "secrets_vault_token_file", "secrets_vault_namespace",
                     "secrets_vault_timeout_s", "environment", "outbound_allowlist",
                     "outbound_blocked_hosts", "outbound_resolve_hosts",
                     "outbound_require_https", "outbound_tls_min_version", "outbound_ca_file"),
        required_keys=("secrets_vault_url", "secrets_vault_token_file"),
    ),
    factory=_vault,
)
