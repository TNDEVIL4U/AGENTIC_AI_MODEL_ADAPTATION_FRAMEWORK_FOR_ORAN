"""API-key authentication and role checks.

Every ``/api/v1`` router except health requires an authenticated caller (any role may read).
Endpoints that change state add ``Depends(require_roles(...))``:

    POST /adaptation/events            ADMIN, OPERATOR, ML_ENGINEER
    POST /datasets, /datasets/{id}/versions, /models/attach
                                       ADMIN, ML_ENGINEER   (data and registry changes)
    POST /models/{id}/rollback         ADMIN, OPERATOR      (moves LIVE)

Keys are compared as SHA-256 digests in constant time; the key itself is never stored or
logged. With ``auth_enabled=False`` (local development and tests) every caller is an ADMIN
named "anonymous"."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from oran_adapt.core.enums import Role
from oran_adapt.core.errors import AdaptationError

READ_ROLES = (Role.ADMIN, Role.OPERATOR, Role.ML_ENGINEER, Role.READ_ONLY)
SUBMIT_ROLES = (Role.ADMIN, Role.OPERATOR, Role.ML_ENGINEER)
DATA_ROLES = (Role.ADMIN, Role.ML_ENGINEER)
PROMOTE_ROLES = (Role.ADMIN, Role.OPERATOR)


class AuthenticationError(AdaptationError):
    code = "UNAUTHENTICATED"


class PermissionDeniedError(AdaptationError):
    code = "FORBIDDEN"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _presented_key(request: Request) -> str | None:
    key = request.headers.get("X-API-Key")
    if key:
        return key
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def authenticate(request: Request) -> Principal:
    """The caller behind this request. Raises AuthenticationError (401) when no valid key was
    sent. The result is cached on ``request.state.principal``."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    principal: Principal | None
    if not settings.auth_enabled:
        principal = Principal(name="anonymous", role=Role.ADMIN)
    else:
        key = _presented_key(request)
        if not key:
            raise AuthenticationError("an API key is required (X-API-Key header)")
        digest = hash_api_key(key)
        principal = None
        # Check every entry, so the time taken does not reveal which one matched.
        for stored, spec in settings.api_keys.items():
            if hmac.compare_digest(stored.lower(), digest):
                role, _, name = spec.partition(":")
                principal = Principal(name=name or f"key-{stored[:8].lower()}", role=Role(role))
        if principal is None:
            raise AuthenticationError("invalid API key")
    request.state.principal = principal
    return principal


def require_roles(*roles: Role) -> Callable[[Request], Principal]:
    """A FastAPI dependency admitting only callers with one of ``roles`` (403 otherwise)."""
    allowed = frozenset(roles)

    def _check(request: Request) -> Principal:
        principal = authenticate(request)
        if principal.role not in allowed:
            raise PermissionDeniedError(
                f"role {principal.role.value} may not do this",
                required=sorted(r.value for r in allowed),
            )
        return principal

    return _check


# Endpoint parameter types for callers that also need to know who is calling (the audit actor).
Submitter = Annotated[Principal, Depends(require_roles(*SUBMIT_ROLES))]
DataEditor = Annotated[Principal, Depends(require_roles(*DATA_ROLES))]
Promoter = Annotated[Principal, Depends(require_roles(*PROMOTE_ROLES))]
