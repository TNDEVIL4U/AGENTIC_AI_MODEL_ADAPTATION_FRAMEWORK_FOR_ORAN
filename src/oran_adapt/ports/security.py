"""Authentication, authorization policy and secrets ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oran_adapt.core.enums import Role


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


@runtime_checkable
class AuthPort(Protocol):
    """Identifies the caller from the request headers (names are case-insensitive). ``peer`` is
    the address of the connection's other end (a proxy, when one is in front); adapters that
    believe identity headers set by a proxy check it against AUTH_TRUSTED_PROXIES. Raises
    AuthenticationError when no valid credential was presented."""

    def authenticate(self, headers: Mapping[str, str], *, peer: str | None = None) -> Principal:
        ...


@runtime_checkable
class PolicyPort(Protocol):
    """Decides which roles may perform an action (``read``, ``submit``, ``data``, ``promote``,
    ``admin``). Raises PermissionDeniedError on refusal."""

    def allowed_roles(self, action: str) -> frozenset[Role]: ...

    def authorize(self, principal: Principal, action: str) -> None: ...


@runtime_checkable
class SecretsPort(Protocol):
    """Looks up a named secret; None when this backend has no value for it."""

    def get(self, name: str) -> str | None: ...
