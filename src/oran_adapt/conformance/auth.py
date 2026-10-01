"""Conformance suites for ``AuthPort`` adapters (who is calling) and ``SecretsPort`` adapters
(where secret values come from).

Each check takes an adapter and a context and raises ConformanceFailure on a deviation::

    @pytest.mark.parametrize("check", sorted(AUTH_CHECKS))
    def test_my_auth(check):
        AUTH_CHECKS[check](MyAuth(...), AuthContext(credentials=..., forgeries=...))

    @pytest.mark.parametrize("check", sorted(SECRETS_CHECKS))
    def test_my_secrets(check):
        SECRETS_CHECKS[check](MySecrets(...), SecretsContext(known={...}))

docs/adapters/auth.md explains each rule.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import AuthenticationError
from oran_adapt.ports import AuthPort, Principal, SecretsPort

# A header value this long is a credential (or part of one) that must never be echoed.
_ECHO_MIN = 8


@dataclass
class AuthContext:
    credentials: Callable[[Role], Mapping[str, str]]
    """Request headers a valid caller holding ``role`` sends."""
    forgeries: Mapping[str, Mapping[str, str]]
    """Named header sets the adapter must refuse (a wrong key, a bad signature, an unknown
    identity, ...)."""
    peer: str | None = None
    """The connection peer to pass (a trusted proxy's address for header-trusting adapters)."""
    roles: tuple[Role, ...] = field(default_factory=lambda: tuple(Role))
    """The roles ``credentials`` can produce."""


def _refused(port: AuthPort, headers: Mapping[str, str], peer: str | None, what: str
             ) -> AuthenticationError:
    try:
        who = port.authenticate(headers, peer=peer)
    except AuthenticationError as exc:
        return exc
    except Exception as exc:
        raise ConformanceFailure(
            f"{what}: raised {type(exc).__name__}, not AuthenticationError") from exc
    raise ConformanceFailure(f"{what}: accepted as {who!r}")


def check_protocol(port: AuthPort, ctx: AuthContext) -> None:
    expect(isinstance(port, AuthPort), f"{type(port).__name__} does not implement AuthPort")


def check_accepts_every_role(port: AuthPort, ctx: AuthContext) -> None:
    for role in ctx.roles:
        who = port.authenticate(ctx.credentials(role), peer=ctx.peer)
        expect(isinstance(who, Principal), f"authenticate returned {type(who).__name__}")
        expect(who.role is role, f"credentials for {role.value} authenticated as {who.role}")
        expect(isinstance(who.name, str) and who.name != "", "the principal has no name")


def check_same_credential_same_principal(port: AuthPort, ctx: AuthContext) -> None:
    headers = ctx.credentials(ctx.roles[0])
    first = port.authenticate(headers, peer=ctx.peer)
    again = port.authenticate(headers, peer=ctx.peer)
    expect(first == again, f"one credential gave two principals: {first!r} then {again!r}")


def check_header_names_case_insensitive(port: AuthPort, ctx: AuthContext) -> None:
    for transform in (str.lower, str.upper):
        headers = {transform(k): v for k, v in ctx.credentials(ctx.roles[0]).items()}
        who = port.authenticate(headers, peer=ctx.peer)
        expect(who.role is ctx.roles[0], f"header names in {transform.__name__} case refused")


def check_no_credential_refused(port: AuthPort, ctx: AuthContext) -> None:
    _refused(port, {}, ctx.peer, "a request with no credential")


def check_forgeries_refused(port: AuthPort, ctx: AuthContext) -> None:
    expect(bool(ctx.forgeries), "the context names no forgery to refuse")
    for name, headers in ctx.forgeries.items():
        _refused(port, headers, ctx.peer, f"forgery {name!r}")


def check_errors_do_not_echo_credentials(port: AuthPort, ctx: AuthContext) -> None:
    for name, headers in ctx.forgeries.items():
        error = _refused(port, headers, ctx.peer, f"forgery {name!r}")
        shown = f"{error} {error.to_dict()!r}"
        for value in headers.values():
            expect(len(value) < _ECHO_MIN or value not in shown,
                   f"the error for forgery {name!r} repeats the presented credential")


AUTH_CHECKS: dict[str, Callable[[AuthPort, AuthContext], None]] = {
    "protocol": check_protocol,
    "accepts_every_role": check_accepts_every_role,
    "same_credential_same_principal": check_same_credential_same_principal,
    "header_names_case_insensitive": check_header_names_case_insensitive,
    "no_credential_refused": check_no_credential_refused,
    "forgeries_refused": check_forgeries_refused,
    "errors_do_not_echo_credentials": check_errors_do_not_echo_credentials,
}


@dataclass
class SecretsContext:
    known: Mapping[str, str]
    """Secret name (setting name, lower case) -> the value the backend holds for it."""
    absent: str = "conformance_absent_secret"
    """A name the backend holds nothing for."""


def check_secrets_protocol(port: SecretsPort, ctx: SecretsContext) -> None:
    expect(isinstance(port, SecretsPort), f"{type(port).__name__} does not implement SecretsPort")


def check_secrets_read_back(port: SecretsPort, ctx: SecretsContext) -> None:
    expect(bool(ctx.known), "the context names no secret to read")
    for name, value in ctx.known.items():
        for spelled in (name.lower(), name.upper()):
            got = port.get(spelled)
            expect(got == value, f"get({spelled!r}) did not return the stored value")


def check_secrets_absent_is_none(port: SecretsPort, ctx: SecretsContext) -> None:
    got = port.get(ctx.absent)
    expect(got is None, f"get({ctx.absent!r}) returned {type(got).__name__}, not None")


SECRETS_CHECKS: dict[str, Callable[[SecretsPort, SecretsContext], None]] = {
    "protocol": check_secrets_protocol,
    "read_back": check_secrets_read_back,
    "absent_is_none": check_secrets_absent_is_none,
}


def run_auth(port: AuthPort, ctx: AuthContext) -> list[str]:
    """Run every auth check in order; returns their names. Stops at the first failure."""
    for check in AUTH_CHECKS.values():
        check(port, ctx)
    return list(AUTH_CHECKS)


def run_secrets(port: SecretsPort, ctx: SecretsContext) -> list[str]:
    """Run every secrets check in order; returns their names. Stops at the first failure."""
    for check in SECRETS_CHECKS.values():
        check(port, ctx)
    return list(SECRETS_CHECKS)
