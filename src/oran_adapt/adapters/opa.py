"""Policy adapter ``opa``: authorization decided by an Open Policy Agent server.

Each question is a POST to OPA's data API, ``{POLICY_OPA_URL}/v1/data/{POLICY_OPA_PATH}``, with
the input ``{"action": ..., "role": ..., "principal": ...}``; the action is allowed only when the
decision's ``result`` is exactly ``true``. An undefined decision (no ``result``), any other value,
an HTTP error or an unreachable server is a refusal: the adapter fails closed, like
``static-rbac`` does for an action it has no entry for.

``allowed_roles(action)`` asks once per role and caches the answer for POLICY_OPA_CACHE_S
seconds, so a policy change in OPA reaches the API within that time (0: ask every time). An
action no role may perform is refused (PermissionDeniedError), which keeps deny-by-default.

A policy that reproduces the default POLICY_ROLES matrix::

    package oran_adapt.authz
    default allow := false
    roles := {"read": {"ADMIN", "OPERATOR", "ML_ENGINEER", "READ_ONLY"}, "submit": {...}, ...}
    allow if input.role in roles[input.action]
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from oran_adapt.core.enums import Role
from oran_adapt.core.errors import ConfigurationError, OutboundBlockedError, PermissionDeniedError
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.ports import AdapterSpec, Capability, Principal

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger(__name__)


class OpaPolicy:
    def __init__(self, url: str, *, path: str, timeout_s: float, cache_s: float,
                 token: SecretStr | None = None, policy: OutboundPolicy | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        segments = [s for s in path.strip("/").split("/") if s]
        if not segments:
            raise ConfigurationError("POLICY_OPA_PATH must name a rule, e.g. "
                                     "oran_adapt/authz/allow", key="policy_opa_path")
        self.url = url.rstrip("/")
        self.endpoint = f"{self.url}/v1/data/" + "/".join(quote(s, safe="") for s in segments)
        # Without a policy from the settings, only the configured OPA host is trusted.
        self.policy = policy or OutboundPolicy([urlsplit(self.url).hostname or ""])
        self.timeout_s = timeout_s
        self.cache_s = cache_s
        self.token = token
        self.transport = transport
        self._cache: dict[str, tuple[float, frozenset[Role]]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> OpaPolicy:
        if not settings.policy_opa_url:
            raise ConfigurationError("POLICY_BACKEND=opa needs POLICY_OPA_URL",
                                     key="policy_opa_url")
        return cls(settings.policy_opa_url, path=settings.policy_opa_path,
                   timeout_s=settings.policy_opa_timeout_s, cache_s=settings.policy_opa_cache_s,
                   token=settings.policy_opa_token, policy=OutboundPolicy.from_settings(settings))

    def _allowed(self, action: str, role: Role, name: str) -> bool:
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        body = {"input": {"action": action, "role": role.value,
                          "principal": {"name": name, "role": role.value}}}
        try:
            with self.policy.client(timeout=self.timeout_s, transport=self.transport) as client:
                response = client.post(self.endpoint, json=body, headers=headers)
            decision: Any = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, OutboundBlockedError, ValueError) as exc:
            logger.warning("OPA could not be asked; refusing", extra={"cause": str(exc)})
            return False
        if decision is None:
            logger.warning("OPA answered HTTP %s; refusing", response.status_code)
            return False
        return isinstance(decision, dict) and decision.get("result") is True

    def allowed_roles(self, action: str) -> frozenset[Role]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(action)
        if cached is not None and now - cached[0] < self.cache_s:
            roles = cached[1]
        else:
            roles = frozenset(r for r in Role if self._allowed(action, r, f"role-{r.value}"))
            with self._lock:
                self._cache[action] = (now, roles)
        if not roles:
            raise PermissionDeniedError(f"the policy engine allows action {action!r} to no role",
                                        action=action)
        return roles

    def authorize(self, principal: Principal, action: str) -> None:
        if not self._allowed(action, principal.role, principal.name):
            raise PermissionDeniedError(
                f"role {principal.role.value} may not do this (policy engine)", action=action)


def _opa(settings: Settings) -> OpaPolicy:
    return OpaPolicy.from_settings(settings)


OPA = AdapterSpec(
    capability=Capability(
        port="policy",
        adapter="opa",
        description="Open Policy Agent data API decides each action; fails closed",
        features=frozenset({"roles", "deny_by_default", "external_engine"}),
        config_keys=("policy_opa_url", "policy_opa_path", "policy_opa_token",
                     "policy_opa_timeout_s", "policy_opa_cache_s"),
        required_keys=("policy_opa_url",),
    ),
    factory=_opa,
)
