"""Outbound HTTP policy: SSRF allowlisting and TLS for every HTTP client the framework builds.

Every adapter that talks HTTP gets its client from ``OutboundPolicy.client`` (or
``outbound_client``). Before each request - the first one and every one a caller builds from a
response - the policy checks the URL:

- **Scheme.** ``http`` or ``https`` only. With OUTBOUND_REQUIRE_HTTPS (default: on in
  production) plain ``http`` is refused except to a host whose configured endpoint is itself an
  ``http://`` URL: the operator wrote that URL, so the operator chose plain HTTP for it.
- **Destination.** A host that is not *trusted* must not be, or resolve to, a non-public
  address: loopback, private (RFC 1918, unique-local), link-local (cloud metadata at
  169.254.169.254), carrier-grade NAT, multicast, reserved or unspecified. Trusted hosts are
  OUTBOUND_ALLOWLIST entries (a host, ``.suffix`` for a domain and its subdomains, or a CIDR)
  and the host of every endpoint the configuration names (any ``*_url``, ``*_uri``,
  ``*_endpoint``, ``*_endpoint_url``, ``*_issuer`` or ``*_issuers`` setting holding
  ``http(s)://`` URLs, and the hosts in DATASET_HTTP_ALLOWED_HOSTS). Names in OUTBOUND_BLOCKED_HOSTS (``localhost``, the metadata
  service names) are refused even when name resolution is off.
- **Redirects** are never followed, so an allowed server cannot bounce a request inward.

TLS: certificates are always verified (there is no switch to turn verification off); the
minimum protocol version is OUTBOUND_TLS_MIN_VERSION; a private CA is given per client
(``ca_file``, e.g. K8S_CA_FILE) or for every client (OUTBOUND_CA_FILE).

A refused request raises OutboundBlockedError (``OUTBOUND_BLOCKED``) before anything is sent.
Limitation: the address is checked when the request is made and resolved again when the
connection opens, so a DNS answer that changes in between (rebinding) is not caught here; pair
the policy with an egress NetworkPolicy/firewall (docs/security.md).
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from oran_adapt.core.errors import OutboundBlockedError

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Setting-name endings that mark a configured endpoint (a naming convention, not a list of
# adapters: a new adapter's ``foo_url`` key is trusted with no change here).
ENDPOINT_SUFFIXES = ("_url", "_uri", "_endpoint", "_endpoint_url", "_issuer", "_issuers")
_TLS_VERSIONS = {"TLSv1.2": ssl.TLSVersion.TLSv1_2, "TLSv1.3": ssl.TLSVersion.TLSv1_3}


def is_public(address: IpAddress) -> bool:
    """True for a globally routable unicast address."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _host_of(value: str) -> tuple[str, str] | None:
    """(scheme, host) of an http(s) URL, or of a bare ``host[:port]``; None otherwise."""
    if "://" not in value:
        host = urlsplit(f"//{value}").hostname
        return ("", host.lower()) if host else None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower()


def configured_endpoints(settings: Settings) -> list[tuple[str, str]]:
    """(scheme, host) of every endpoint the configuration names."""
    found: list[tuple[str, str]] = []
    for name in type(settings).model_fields:
        if not name.endswith(ENDPOINT_SUFFIXES):
            continue
        value: Any = getattr(settings, name, None)
        values = ([*value.keys(), *value.values()] if isinstance(value, dict)
                  else list(value) if isinstance(value, list) else [value])
        for item in values:
            if isinstance(item, SecretStr):
                item = item.get_secret_value()
            endpoint = _host_of(item) if isinstance(item, str) and "://" in item else None
            if endpoint:
                found.append(endpoint)
    for entry in getattr(settings, "dataset_http_allowed_hosts", None) or []:
        endpoint = _host_of(entry)
        if endpoint:
            found.append(endpoint)
    return found


class OutboundPolicy:
    """Which URLs the framework may call, and the TLS settings it calls them with. Picklable
    (clients are not): adapters that are pickled into job payloads keep the policy and rebuild
    their client from it."""

    def __init__(
        self,
        allowlist: Iterable[str] = (),
        *,
        plain_http_hosts: Iterable[str] = (),
        blocked_hosts: Iterable[str] = (),
        resolve_hosts: bool = True,
        require_https: bool = False,
        tls_min_version: str = "TLSv1.2",
        ca_file: str | None = None,
    ) -> None:
        self.hosts: set[str] = set()
        self.suffixes: list[str] = []
        self.networks: list[IpNetwork] = []
        for raw in allowlist:
            entry = raw.strip().lower()
            if not entry:
                continue
            if entry.startswith("."):
                self.suffixes.append(entry)
                continue
            try:
                self.networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                self.hosts.add(entry.strip("[]"))
        self.plain_http_hosts = {h.lower() for h in plain_http_hosts}
        self.blocked_hosts = {h.lower() for h in blocked_hosts}
        self.resolve_hosts = resolve_hosts
        self.require_https = require_https
        if tls_min_version not in _TLS_VERSIONS:
            raise ValueError(f"unknown TLS version {tls_min_version!r}")
        self.tls_min_version = tls_min_version
        self.ca_file = ca_file

    @classmethod
    def from_settings(cls, settings: Settings) -> OutboundPolicy:
        endpoints = configured_endpoints(settings)
        require = settings.outbound_require_https
        return cls(
            [*settings.outbound_allowlist, *(host for _, host in endpoints)],
            plain_http_hosts=[host for scheme, host in endpoints if scheme == "http"],
            blocked_hosts=settings.outbound_blocked_hosts,
            resolve_hosts=settings.outbound_resolve_hosts,
            require_https=settings.environment == "production" if require is None else require,
            tls_min_version=settings.outbound_tls_min_version,
            ca_file=settings.outbound_ca_file,
        )

    # ---- the URL check -------------------------------------------------------------------
    def trusted(self, host: str) -> bool:
        host = host.lower().strip("[]")
        if host in self.hosts or any(
            host.endswith(s) or host == s[1:] for s in self.suffixes
        ):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in net for net in self.networks)

    def _addresses(self, host: str) -> list[IpAddress]:
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        if not self.resolve_hosts:
            return []
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return []  # unresolvable: the connection fails on its own; nothing to reach
        return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]

    def check(self, url: str | httpx.URL) -> None:
        """Raise OutboundBlockedError unless this policy lets the framework call ``url``."""
        parts = urlsplit(str(url))
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        # The URL itself never goes into the error: its query string may carry a credential.
        where = f"{scheme}://{host}"
        if scheme not in ("http", "https") or not host:
            raise OutboundBlockedError("only http and https URLs may be called", url=where)
        if scheme == "http" and self.require_https and host not in self.plain_http_hosts:
            raise OutboundBlockedError(
                "plain http is refused (OUTBOUND_REQUIRE_HTTPS); use https", url=where
            )
        if self.trusted(host):
            return
        if host in self.blocked_hosts or any(
            host.endswith("." + b) for b in self.blocked_hosts
        ):
            raise OutboundBlockedError(
                "host is on OUTBOUND_BLOCKED_HOSTS", url=where, key="OUTBOUND_ALLOWLIST"
            )
        for address in self._addresses(host):
            if not is_public(address):
                raise OutboundBlockedError(
                    "destination is not a public address; add it to OUTBOUND_ALLOWLIST if "
                    "it is meant to be reached",
                    url=where,
                    address=str(address),
                    key="OUTBOUND_ALLOWLIST",
                )

    def _on_request(self, request: httpx.Request) -> None:
        self.check(request.url)

    # ---- clients -------------------------------------------------------------------------
    def ssl_context(self, ca_file: str | None = None) -> ssl.SSLContext:
        return _ssl_context(ca_file or self.ca_file, self.tls_min_version)

    def client(
        self,
        *,
        timeout: float,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Client:
        """An httpx client that checks every request against this policy, verifies TLS and never
        follows redirects."""
        client: httpx.Client = self.client_from(
            httpx, timeout=timeout, ca_file=ca_file, transport=transport, headers=headers
        )
        return client

    def client_from(
        self,
        module: Any,
        *,
        timeout: float,
        ca_file: str | None = None,
        transport: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """``client`` built from ``module``, any httpx-compatible library (an SDK that ships
        its own fork of httpx, such as ``httpx2``, is handed a client of that fork)."""
        return module.Client(
            timeout=timeout,
            follow_redirects=False,
            verify=self.ssl_context(ca_file),
            transport=transport,
            headers=headers,
            event_hooks={"request": [self._on_request]},
        )


@lru_cache(maxsize=16)
def _ssl_context(ca_file: str | None, min_version: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = _TLS_VERSIONS[min_version]
    return context


def outbound_client(
    settings: Settings,
    *,
    timeout: float,
    ca_file: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """A policy-checked client for ``settings`` (see OutboundPolicy)."""
    return OutboundPolicy.from_settings(settings).client(
        timeout=timeout, ca_file=ca_file, transport=transport
    )
