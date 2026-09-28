"""Auth adapter ``api-key`` and policy adapter ``static-rbac``.

``api-key``: callers send the key in the configured header (default ``X-API-Key``) or as
``Authorization: Bearer <key>``. Keys are never stored: API_KEYS maps the SHA-256 hex digest of
each key to "ROLE" or "ROLE:caller-name". Digests are compared in constant time and every entry
is checked, so the time taken does not reveal which one matched. With no keys configured every
request is refused (fail closed).

``static-rbac``: a fixed action -> roles matrix from POLICY_ROLES.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING

from oran_adapt.core.enums import Role
from oran_adapt.core.errors import AuthenticationError, ConfigurationError, PermissionDeniedError
from oran_adapt.ports import AdapterSpec, Capability, Principal

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class ApiKeyAuth:
    def __init__(self, api_keys: Mapping[str, str], header: str) -> None:
        self._keys = {digest.lower(): spec for digest, spec in api_keys.items()}
        self.header = header

    def presented_key(self, headers: Mapping[str, str]) -> str | None:
        key = _header(headers, self.header)
        if key:
            return key
        scheme, _, token = (_header(headers, "Authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
        return None

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        key = self.presented_key(headers)
        if not key:
            raise AuthenticationError(f"an API key is required ({self.header} header)")
        digest = hash_api_key(key)
        principal: Principal | None = None
        for stored, spec in self._keys.items():
            if hmac.compare_digest(stored, digest):
                role, _, name = spec.partition(":")
                principal = Principal(name=name or f"key-{stored[:8]}", role=Role(role))
        if principal is None:
            raise AuthenticationError("invalid API key")
        return principal


class StaticRbacPolicy:
    def __init__(self, matrix: Mapping[str, list[str]]) -> None:
        self._matrix: dict[str, frozenset[Role]] = {}
        for action, roles in matrix.items():
            try:
                self._matrix[action] = frozenset(Role(r) for r in roles)
            except ValueError as exc:
                raise ConfigurationError(
                    f"POLICY_ROLES[{action!r}] names an unknown role",
                    key="POLICY_ROLES",
                    roles=list(roles),
                    known=[r.value for r in Role],
                ) from exc

    def allowed_roles(self, action: str) -> frozenset[Role]:
        try:
            return self._matrix[action]
        except KeyError:
            # Deny by default: an action nobody configured is allowed to no one.
            raise PermissionDeniedError(
                f"action {action!r} has no policy entry", action=action
            ) from None

    def authorize(self, principal: Principal, action: str) -> None:
        allowed = self.allowed_roles(action)
        if principal.role not in allowed:
            raise PermissionDeniedError(
                f"role {principal.role.value} may not do this",
                required=sorted(r.value for r in allowed),
            )


def _api_key(settings: Settings) -> ApiKeyAuth:
    return ApiKeyAuth(settings.api_keys, settings.auth_api_key_header)


def _static_rbac(settings: Settings) -> StaticRbacPolicy:
    return StaticRbacPolicy(settings.policy_roles)


API_KEY = AdapterSpec(
    capability=Capability(
        port="auth",
        adapter="api-key",
        description="SHA-256-digested API keys from API_KEYS, sent in a header or as Bearer",
        features=frozenset({"static_keys", "roles"}),
        config_keys=("api_keys", "auth_api_key_header"),
    ),
    factory=_api_key,
)

STATIC_RBAC = AdapterSpec(
    capability=Capability(
        port="policy",
        adapter="static-rbac",
        description="fixed action -> roles matrix from POLICY_ROLES, deny by default",
        features=frozenset({"roles", "deny_by_default"}),
        config_keys=("policy_roles",),
    ),
    factory=_static_rbac,
)
