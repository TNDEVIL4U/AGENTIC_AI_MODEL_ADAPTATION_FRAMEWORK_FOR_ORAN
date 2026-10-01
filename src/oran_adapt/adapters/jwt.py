"""JSON Web Token verification against a JSON Web Key Set (RFC 7515/7517/7519), for the ``oidc``
and ``gateway`` auth adapters. Built on ``cryptography``; no JWT library is needed.

What a token must satisfy to be accepted:
- a compact JWS with ``alg`` in the configured allowlist (RS256/384/512, PS256/384/512,
  ES256/384/512; never ``none`` and never an HMAC algorithm, so a public key can not be used as
  a shared secret);
- a ``kid`` that names a key in the issuer's JWKS, of the algorithm's key type (a token with no
  ``kid`` is tried against every key of that type);
- a valid signature;
- ``iss`` equal to the expected issuer, ``aud`` containing the expected audience (when one is
  configured), ``exp`` in the future and ``nbf``/``iat`` not in the future, all with
  ``leeway_s`` of clock skew.

The JWKS is fetched through the outbound policy (SSRF-checked, TLS-verified), cached for
``cache_ttl_s``, and refetched at most once per ``min_refetch_s`` when a token names an unknown
``kid`` (key rotation), so a stream of forged ``kid``s cannot hammer the issuer.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from oran_adapt.core.errors import AuthenticationError, OutboundBlockedError

_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512,
}
_CURVES: dict[str, ec.EllipticCurve] = {
    "P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1(),
}
SUPPORTED_ALGORITHMS = frozenset(
    f"{family}{bits}" for family in ("RS", "PS", "ES") for bits in _HASHES
)
PublicKey = rsa.RSAPublicKey | ec.EllipticCurvePublicKey


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _int(value: str) -> int:
    return int.from_bytes(b64url_decode(value), "big")


def public_key(jwk: Mapping[str, Any]) -> PublicKey:
    """The public key a JWK describes (``RSA`` or ``EC``). ValueError on anything else."""
    kty = jwk.get("kty")
    if kty == "RSA":
        return rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
    if kty == "EC":
        curve = _CURVES.get(str(jwk.get("crv")))
        if curve is None:
            raise ValueError(f"unsupported EC curve {jwk.get('crv')!r}")
        return ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), curve).public_key()
    raise ValueError(f"unsupported key type {kty!r}")


def _verify_signature(alg: str, key: PublicKey, signed: bytes, signature: bytes) -> None:
    digest = _HASHES[alg[2:]]()
    if alg.startswith("ES"):
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise InvalidSignature
        half = len(signature) // 2
        if half == 0 or len(signature) % 2:
            raise InvalidSignature
        der = encode_dss_signature(
            int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big")
        )
        key.verify(der, signed, ec.ECDSA(digest))
        return
    if not isinstance(key, rsa.RSAPublicKey):
        raise InvalidSignature
    pad: padding.AsymmetricPadding = (
        padding.PSS(mgf=padding.MGF1(digest), salt_length=padding.PSS.DIGEST_LENGTH)
        if alg.startswith("PS")
        else padding.PKCS1v15()
    )
    key.verify(signature, signed, pad, digest)


class JwksCache:
    """An issuer's signing keys, fetched from ``url`` with ``client_factory``'s client."""

    def __init__(
        self,
        url: str,
        client_factory: Callable[[], httpx.Client],
        *,
        cache_ttl_s: float,
        min_refetch_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.client_factory = client_factory
        self.cache_ttl_s = cache_ttl_s
        self.min_refetch_s = min_refetch_s
        self.clock = clock
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        try:
            with self.client_factory() as client:
                response = client.get(self.url, headers={"Accept": "application/json"})
            response.raise_for_status()
            keys = response.json()["keys"]
        except (httpx.HTTPError, OutboundBlockedError, ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError(
                "the token issuer's signing keys could not be fetched",
                jwks_url=self.url, cause=type(exc).__name__,
            ) from exc
        self._keys = [k for k in keys if isinstance(k, dict)]
        self._fetched_at = self.clock()

    def keys(self, kid: str | None) -> list[dict[str, Any]]:
        """Keys matching ``kid`` (all keys when ``kid`` is None). Fetches when the cache is
        empty or stale, and once more when ``kid`` is unknown and the last fetch is older than
        ``min_refetch_s``."""
        with self._lock:
            now = self.clock()
            if self._fetched_at is None or now - self._fetched_at > self.cache_ttl_s:
                self._fetch()
            found = [k for k in self._keys if kid is None or k.get("kid") == kid]
            age = now - (self._fetched_at or now)
            if not found and kid is not None and age >= self.min_refetch_s:
                self._fetch()
                found = [k for k in self._keys if k.get("kid") == kid]
            return found


@dataclass(frozen=True)
class TokenRules:
    issuer: str
    audience: str | None
    algorithms: frozenset[str]
    leeway_s: float
    required_claims: tuple[str, ...] = ("exp",)


def _segments(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("malformed token")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except ValueError as exc:
        raise AuthenticationError("malformed token") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AuthenticationError("malformed token")
    return header, claims, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def unverified_claims(token: str) -> dict[str, Any]:
    """The claims of ``token`` WITHOUT verification - only for choosing which issuer's rules to
    verify it with."""
    return _segments(token)[1]


def verify(
    token: str,
    rules: TokenRules,
    jwks: JwksCache,
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The verified claims of ``token``; AuthenticationError (never the token) otherwise."""
    header, claims, signed, signature = _segments(token)
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in rules.algorithms or alg not in SUPPORTED_ALGORITHMS:
        raise AuthenticationError("token algorithm not allowed", alg=str(alg))
    kid = header.get("kid")
    kty = "EC" if alg.startswith("ES") else "RSA"
    candidates = [k for k in jwks.keys(kid if isinstance(kid, str) else None)
                  if k.get("kty") == kty and k.get("use", "sig") == "sig"
                  and k.get("alg", alg) == alg]
    if not candidates:
        raise AuthenticationError("token signed by an unknown key", kid=str(kid))
    for jwk in candidates:
        try:
            _verify_signature(alg, public_key(jwk), signed, signature)
            break
        except (InvalidSignature, ValueError, KeyError, TypeError):
            continue
    else:
        raise AuthenticationError("token signature is invalid")
    _check_claims(claims, rules, now())
    return claims


def _number(claims: Mapping[str, Any], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AuthenticationError(f"token claim {name!r} is not a number")
    return float(value)


def _check_claims(claims: Mapping[str, Any], rules: TokenRules, now: float) -> None:
    for name in rules.required_claims:
        if name not in claims:
            raise AuthenticationError(f"token has no {name!r} claim")
    if claims.get("iss") != rules.issuer:
        raise AuthenticationError("token issuer not accepted")
    if rules.audience is not None:
        aud = claims.get("aud")
        audiences: Sequence[Any] = aud if isinstance(aud, list) else [aud]
        if rules.audience not in audiences:
            raise AuthenticationError("token audience not accepted")
    exp = _number(claims, "exp")
    if exp is not None and now > exp + rules.leeway_s:
        raise AuthenticationError("token expired")
    for name in ("nbf", "iat"):
        at = _number(claims, name)
        if at is not None and at > now + rules.leeway_s:
            raise AuthenticationError(f"token {name!r} is in the future")


def claim(claims: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted claim path (``realm_access.roles``); None when absent."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value
