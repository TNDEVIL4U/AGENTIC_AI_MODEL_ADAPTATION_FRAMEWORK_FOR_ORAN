"""An auth adapter template: opaque bearer tokens looked up in a JSON file of token digests.

The file maps the SHA-256 hex digest of each token to ``"ROLE"`` or ``"ROLE:name"``::

    {"9f86d081884c7d65...": "OPERATOR:noc-dashboard", "60303ae22b998861...": "READ_ONLY"}

so the file never holds a usable token. Replace ``_lookup()`` with your identity system (an
OAuth2 token introspection endpoint, an LDAP bind, a vendor SDK; import its SDK inside the
adapter and build HTTP clients with ``oran_adapt.core.outbound.outbound_client``) and keep
``authenticate``'s rules (docs/adapters/auth.md): header names are case-insensitive, a missing
or unknown credential raises AuthenticationError, and no error message repeats the credential.

Register it in your package's ``pyproject.toml``::

    [project.entry-points."oran_adapt.auth"]
    token-file = "my_package.adapter:SPEC"

then set ``AUTH_BACKEND=token-file`` and ``AUTH_TOKEN_FILE``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from typing import Any

from oran_adapt.core.enums import Role
from oran_adapt.core.errors import AuthenticationError, ConfigurationError
from oran_adapt.ports import AdapterSpec, Capability, Principal


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenFileAuth:
    def __init__(self, path: str, header: str = "Authorization") -> None:
        self.path = path
        self.header = header
        try:
            with open(path, encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ConfigurationError("AUTH_TOKEN_FILE could not be read", key="AUTH_TOKEN_FILE",
                                     cause=type(exc).__name__) from exc
        self._entries = {str(k).lower(): str(v) for k, v in dict(entries).items()}

    def _presented(self, headers: Mapping[str, str]) -> str | None:
        for key, value in headers.items():
            if key.lower() == self.header.lower():
                scheme, _, token = value.partition(" ")
                return token.strip() if scheme.lower() == "bearer" and token else None
        return None

    def _lookup(self, token: str) -> str | None:
        wanted = digest(token)
        found = None
        for stored, spec in self._entries.items():
            if hmac.compare_digest(stored, wanted):  # constant time: no timing oracle
                found = spec
        return found

    def authenticate(self, headers: Mapping[str, str], *, peer: str | None = None) -> Principal:
        token = self._presented(headers)
        if not token:
            raise AuthenticationError(f"a bearer token is required ({self.header} header)")
        spec = self._lookup(token)
        if spec is None:
            raise AuthenticationError("invalid bearer token")
        role, _, name = spec.partition(":")
        return Principal(name=name or f"token-{digest(token)[:8]}", role=Role(role))


def _factory(settings: Any) -> TokenFileAuth:
    # Settings ignores unknown keys; a plugin reads its own from the environment.
    return TokenFileAuth(os.environ.get("AUTH_TOKEN_FILE", "tokens.json"))


SPEC = AdapterSpec(
    capability=Capability(
        port="auth",
        adapter="token-file",
        description="opaque bearer tokens checked against a JSON file of SHA-256 digests",
        features=frozenset({"static_keys", "roles"}),
    ),
    factory=_factory,
)
