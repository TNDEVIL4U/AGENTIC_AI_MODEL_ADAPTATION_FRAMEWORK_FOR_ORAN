"""Auth adapters ``oidc``, ``gateway`` and ``mtls`` (``api-key`` is in adapters/access.py).

``oidc``: callers send ``Authorization: Bearer <JWT>`` issued by AUTH_OIDC_ISSUER for
AUTH_OIDC_AUDIENCE. Keys come from AUTH_OIDC_JWKS_URL, or from the issuer's discovery document
(``<issuer>/.well-known/openid-configuration``) when that is unset. The caller's roles are the
values of the AUTH_ROLE_CLAIM claim (a dotted path, a list or a string) mapped through
AUTH_ROLE_MAP (claim value -> ADMIN/OPERATOR/ML_ENGINEER/READ_ONLY); with several, the most
privileged wins. Its name is the AUTH_NAME_CLAIM claim.

``gateway``: an API gateway in front of the service authenticates callers and forwards a signed
JWT assertion in AUTH_GATEWAY_HEADER. The assertion is accepted only from a peer inside
AUTH_TRUSTED_PROXIES and only when its issuer is a key of AUTH_GATEWAY_ISSUERS (issuer -> JWKS
URL); it is then verified like an OIDC token.

``mtls``: TLS with client certificates is terminated by a proxy (ingress, Envoy, nginx) that
forwards the verified client certificate in AUTH_MTLS_CERT_HEADER (URL-encoded PEM, or an Envoy
``x-forwarded-client-cert`` element with ``Cert="..."``). The header is read only from a peer in
AUTH_TRUSTED_PROXIES; the certificate must be directly issued by a CA in AUTH_MTLS_CA_FILE and
inside its validity period, and its identity (subject CN, then DNS and URI SANs) must be a key
of AUTH_MTLS_IDENTITIES (identity -> role).

Every failure is AuthenticationError (401) whose message never contains the credential.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from collections.abc import Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.x509.oid import NameOID

from oran_adapt.adapters.jwt import JwksCache, TokenRules, claim, unverified_claims, verify
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import (
    AuthenticationError,
    ConfigurationError,
    OutboundBlockedError,
    PermissionDeniedError,
)
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability, Principal

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

_PRIVILEGE = list(Role)  # declaration order: most privileged first


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def bearer(headers: Mapping[str, str], name: str = "Authorization") -> str | None:
    value = _header(headers, name)
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if token and scheme.lower() == "bearer":
        return token.strip()
    return value.strip() if name.lower() != "authorization" else None


def parse_role_map(mapping: Mapping[str, str], key: str) -> dict[str, Role]:
    try:
        return {external: Role(role) for external, role in mapping.items()}
    except ValueError as exc:
        raise ConfigurationError(
            f"{key.upper()} maps to an unknown role", key=key.upper(),
            known=[r.value for r in Role],
        ) from exc


class ClaimsMapper:
    """Claims -> Principal through a role claim, a role map and a name claim."""

    def __init__(self, role_claim: str, role_map: Mapping[str, Role], name_claim: str) -> None:
        self.role_claim = role_claim
        self.role_map = dict(role_map)
        self.name_claim = name_claim

    def principal(self, claims: Mapping[str, Any]) -> Principal:
        raw = claim(claims, self.role_claim)
        values = raw if isinstance(raw, list) else [raw] if raw is not None else []
        roles = {self.role_map[v] for v in values if isinstance(v, str) and v in self.role_map}
        if not roles:
            raise PermissionDeniedError("the token grants no role this service knows",
                                        role_claim=self.role_claim)
        name = claim(claims, self.name_claim)
        role = min(roles, key=_PRIVILEGE.index)
        return Principal(name=str(name) if name else "unnamed", role=role)


class TrustedProxies:
    def __init__(self, cidrs: Sequence[str]) -> None:
        self.networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]

    def check(self, peer: str | None, what: str) -> None:
        try:
            address = ipaddress.ip_address(peer or "")
        except ValueError:
            address = None
        if address is None or not any(address in net for net in self.networks):
            raise AuthenticationError(f"{what} is accepted only from AUTH_TRUSTED_PROXIES")


class OidcAuth:
    def __init__(
        self,
        rules: TokenRules,
        mapper: ClaimsMapper,
        policy: OutboundPolicy,
        *,
        jwks_url: str | None,
        timeout_s: float,
        cache_ttl_s: float,
        min_refetch_s: float,
    ) -> None:
        self.rules = rules
        self.mapper = mapper
        self._client = partial(policy.client, timeout=timeout_s)
        self._jwks_url = jwks_url
        self._cache_ttl_s = cache_ttl_s
        self._min_refetch_s = min_refetch_s
        self._jwks: JwksCache | None = None

    def _discover(self) -> str:
        url = self.rules.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            with self._client() as client:
                response = client.get(url)
            response.raise_for_status()
            jwks_uri = response.json()["jwks_uri"]
        except (httpx.HTTPError, OutboundBlockedError, ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError("the OIDC discovery document could not be read",
                                      url=url, cause=type(exc).__name__) from exc
        return str(jwks_uri)

    @property
    def jwks(self) -> JwksCache:
        if self._jwks is None:
            self._jwks = JwksCache(self._jwks_url or self._discover(), self._client,
                                   cache_ttl_s=self._cache_ttl_s,
                                   min_refetch_s=self._min_refetch_s)
        return self._jwks

    def authenticate(self, headers: Mapping[str, str], *, peer: str | None = None) -> Principal:
        token = bearer(headers)
        if not token:
            raise AuthenticationError("a bearer token is required (Authorization header)")
        return self.mapper.principal(verify(token, self.rules, self.jwks))


class GatewayAuth:
    def __init__(
        self,
        issuers: Mapping[str, str],
        *,
        header: str,
        audience: str | None,
        algorithms: frozenset[str],
        leeway_s: float,
        mapper: ClaimsMapper,
        proxies: TrustedProxies,
        policy: OutboundPolicy,
        timeout_s: float,
        cache_ttl_s: float,
        min_refetch_s: float,
    ) -> None:
        self.header = header
        self.mapper = mapper
        self.proxies = proxies
        client = partial(policy.client, timeout=timeout_s)
        self.issuers = {
            issuer: (
                TokenRules(issuer, audience, algorithms, leeway_s),
                JwksCache(url, client, cache_ttl_s=cache_ttl_s, min_refetch_s=min_refetch_s),
            )
            for issuer, url in issuers.items()
        }

    def authenticate(self, headers: Mapping[str, str], *, peer: str | None = None) -> Principal:
        token = bearer(headers, self.header)
        if not token:
            raise AuthenticationError(f"a gateway assertion is required ({self.header} header)")
        self.proxies.check(peer, "a gateway assertion")
        issuer = unverified_claims(token).get("iss")
        if not isinstance(issuer, str) or issuer not in self.issuers:
            raise AuthenticationError("gateway assertion issuer not accepted")
        rules, jwks = self.issuers[issuer]
        return self.mapper.principal(verify(token, rules, jwks))


def forwarded_certificate(value: str) -> x509.Certificate:
    """The client certificate in a forwarded-certificate header value."""
    text = value
    if 'Cert="' in text:  # Envoy x-forwarded-client-cert: By=...;Hash=...;Cert="...";...
        text = text.split('Cert="', 1)[1].split('"', 1)[0]
    pem = unquote(text).strip()
    try:
        return x509.load_pem_x509_certificate(pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AuthenticationError("the forwarded client certificate is not valid PEM") from exc


def certificate_identities(cert: x509.Certificate) -> list[str]:
    names = [str(a.value) for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return names
    names += [str(v) for v in san.get_values_for_type(x509.DNSName)]
    names += [str(v) for v in san.get_values_for_type(x509.UniformResourceIdentifier)]
    return names


class MtlsAuth:
    def __init__(
        self,
        cas: Sequence[x509.Certificate],
        identities: Mapping[str, Role],
        *,
        header: str,
        proxies: TrustedProxies,
    ) -> None:
        self.cas = list(cas)
        self.identities = dict(identities)
        self.header = header
        self.proxies = proxies

    def authenticate(self, headers: Mapping[str, str], *, peer: str | None = None) -> Principal:
        value = _header(headers, self.header)
        if not value:
            raise AuthenticationError(f"a client certificate is required ({self.header})")
        self.proxies.check(peer, "a forwarded client certificate")
        cert = forwarded_certificate(value)
        now = dt.datetime.now(dt.UTC)
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise AuthenticationError("client certificate is outside its validity period")
        for ca in self.cas:
            try:
                cert.verify_directly_issued_by(ca)
                break
            except (ValueError, TypeError, InvalidSignature):
                continue
        else:
            raise AuthenticationError("client certificate is not issued by AUTH_MTLS_CA_FILE")
        for identity in certificate_identities(cert):
            if identity in self.identities:
                return Principal(name=identity, role=self.identities[identity])
        raise AuthenticationError("client certificate identity is not in AUTH_MTLS_IDENTITIES")


# ---- factories ------------------------------------------------------------------------------
def _mapper(settings: Settings) -> ClaimsMapper:
    return ClaimsMapper(settings.auth_role_claim,
                        parse_role_map(settings.auth_role_map, "auth_role_map"),
                        settings.auth_name_claim)


def _oidc(settings: Settings) -> OidcAuth:
    issuer = settings.auth_oidc_issuer
    if not issuer:
        raise ConfigurationError("AUTH_BACKEND=oidc requires AUTH_OIDC_ISSUER",
                                 key="AUTH_OIDC_ISSUER")
    return OidcAuth(
        TokenRules(issuer, settings.auth_oidc_audience,
                   frozenset(settings.auth_jwt_algorithms), settings.auth_jwt_leeway_s),
        _mapper(settings),
        OutboundPolicy.from_settings(settings),
        jwks_url=settings.auth_oidc_jwks_url,
        timeout_s=settings.auth_http_timeout_s,
        cache_ttl_s=settings.auth_jwks_cache_ttl_s,
        min_refetch_s=settings.auth_jwks_min_refetch_s,
    )


def _gateway(settings: Settings) -> GatewayAuth:
    return GatewayAuth(
        settings.auth_gateway_issuers,
        header=settings.auth_gateway_header,
        audience=settings.auth_gateway_audience,
        algorithms=frozenset(settings.auth_jwt_algorithms),
        leeway_s=settings.auth_jwt_leeway_s,
        mapper=_mapper(settings),
        proxies=TrustedProxies(settings.auth_trusted_proxies),
        policy=OutboundPolicy.from_settings(settings),
        timeout_s=settings.auth_http_timeout_s,
        cache_ttl_s=settings.auth_jwks_cache_ttl_s,
        min_refetch_s=settings.auth_jwks_min_refetch_s,
    )


def _load_cas(path: str) -> list[x509.Certificate]:
    try:
        with open(path, "rb") as fh:
            return x509.load_pem_x509_certificates(fh.read())
    except (OSError, ValueError) as exc:
        raise ConfigurationError("AUTH_MTLS_CA_FILE cannot be read as PEM certificates",
                                 key="AUTH_MTLS_CA_FILE", path=path) from exc


def _mtls(settings: Settings) -> MtlsAuth:
    ca_file = settings.auth_mtls_ca_file
    if not ca_file:
        raise ConfigurationError("AUTH_BACKEND=mtls requires AUTH_MTLS_CA_FILE",
                                 key="AUTH_MTLS_CA_FILE")
    return MtlsAuth(
        _load_cas(ca_file),
        parse_role_map(settings.auth_mtls_identities, "auth_mtls_identities"),
        header=settings.auth_mtls_cert_header,
        proxies=TrustedProxies(settings.auth_trusted_proxies),
    )


_JWT_KEYS = ("auth_jwt_algorithms", "auth_jwt_leeway_s", "auth_role_claim", "auth_role_map",
             "auth_name_claim", "auth_http_timeout_s", "auth_jwks_cache_ttl_s",
             "auth_jwks_min_refetch_s")

OIDC = AdapterSpec(
    capability=Capability(
        port="auth",
        adapter="oidc",
        description="OIDC/OAuth2 bearer JWTs verified against the issuer's JWKS",
        features=frozenset({"jwt", "jwks", "roles", "key_rotation"}),
        config_keys=("auth_oidc_issuer", "auth_oidc_audience", "auth_oidc_jwks_url",
                     *_JWT_KEYS),
        required_keys=("auth_oidc_issuer", "auth_role_map"),
    ),
    factory=_oidc,
)

GATEWAY = AdapterSpec(
    capability=Capability(
        port="auth",
        adapter="gateway",
        description="JWT assertions from an API gateway: allowlisted issuers, trusted proxies",
        features=frozenset({"jwt", "jwks", "roles", "trusted_proxy"}),
        config_keys=("auth_gateway_issuers", "auth_gateway_header", "auth_gateway_audience",
                     "auth_trusted_proxies", *_JWT_KEYS),
        required_keys=("auth_gateway_issuers", "auth_trusted_proxies", "auth_role_map"),
    ),
    factory=_gateway,
)

MTLS = AdapterSpec(
    capability=Capability(
        port="auth",
        adapter="mtls",
        description="client certificates forwarded by a TLS-terminating proxy, CA-checked",
        features=frozenset({"client_certificates", "roles", "trusted_proxy"}),
        config_keys=("auth_mtls_ca_file", "auth_mtls_cert_header", "auth_mtls_identities",
                     "auth_trusted_proxies"),
        required_keys=("auth_mtls_ca_file", "auth_mtls_identities", "auth_trusted_proxies"),
    ),
    factory=_mtls,
)
