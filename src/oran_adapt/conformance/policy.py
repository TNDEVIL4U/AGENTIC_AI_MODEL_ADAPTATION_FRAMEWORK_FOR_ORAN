"""Conformance suite for ``PolicyPort`` adapters (which roles may perform an action).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation::

    for check in CHECKS.values():
        check(MyPolicy(...), Context())

``actions`` are the actions the API asks about (the default is every one it uses). Rules: every
such action has an answer, a frozenset of roles; ``authorize`` agrees with ``allowed_roles`` for
every role; an action nobody configured is refused to every role, ADMIN included (deny by
default); a refusal is PermissionDeniedError.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import PermissionDeniedError
from oran_adapt.ports import PolicyPort, Principal

API_ACTIONS = ("read", "submit", "data", "promote", "admin")
_UNKNOWN = "conformance-unconfigured-action"


@dataclass
class Context:
    actions: tuple[str, ...] = API_ACTIONS


def _principal(role: Role) -> Principal:
    return Principal(name=f"conformance-{role.value.lower()}", role=role)


def _refused(action: Callable[[], object], what: str) -> None:
    try:
        action()
    except PermissionDeniedError:
        return
    except Exception as exc:  # any other class is the deviation reported
        raise ConformanceFailure(
            f"{what} must raise PermissionDeniedError, not {type(exc).__name__}") from exc
    raise ConformanceFailure(f"{what} must be refused")


def check_protocol(port: PolicyPort, ctx: Context) -> None:
    expect(isinstance(port, PolicyPort), "does not implement PolicyPort")


def check_every_action_answered(port: PolicyPort, ctx: Context) -> None:
    for action in ctx.actions:
        roles = port.allowed_roles(action)
        expect(isinstance(roles, frozenset) and all(isinstance(r, Role) for r in roles),
               f"allowed_roles({action!r}) must be a frozenset of Role")


def check_authorize_agrees(port: PolicyPort, ctx: Context) -> None:
    for action in ctx.actions:
        allowed = port.allowed_roles(action)
        for role in Role:
            if role in allowed:
                port.authorize(_principal(role), action)
            else:
                _refused(functools.partial(port.authorize, _principal(role), action),
                         f"{role.value} doing {action!r} (not in allowed_roles)")


def check_deny_by_default(port: PolicyPort, ctx: Context) -> None:
    _refused(lambda: port.allowed_roles(_UNKNOWN), "allowed_roles of an unconfigured action")
    for role in Role:
        _refused(functools.partial(port.authorize, _principal(role), _UNKNOWN),
                 f"{role.value} doing an unconfigured action")


def check_stable(port: PolicyPort, ctx: Context) -> None:
    for action in ctx.actions:
        expect(port.allowed_roles(action) == port.allowed_roles(action),
               f"allowed_roles({action!r}) must give the same answer every time")


CHECKS: dict[str, Callable[[PolicyPort, Context], None]] = {
    "protocol": check_protocol,
    "every_action_answered": check_every_action_answered,
    "authorize_agrees": check_authorize_agrees,
    "deny_by_default": check_deny_by_default,
    "stable": check_stable,
}


def run(port: PolicyPort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["API_ACTIONS", "CHECKS", "ConformanceFailure", "Context", "run"]
