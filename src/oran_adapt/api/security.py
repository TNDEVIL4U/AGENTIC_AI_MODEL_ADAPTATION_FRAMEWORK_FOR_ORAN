"""API authentication and authorization, through the AuthPort and PolicyPort adapters that
AUTH_BACKEND and POLICY_BACKEND select (oran_adapt.bootstrap; ``app.state.auth`` and
``app.state.policy``).

Deny by default: every route must declare its policy, either ``public`` (liveness and readiness
only) or the actions it needs through ``require``; a route with neither stops the application
from starting (``enforce_route_policies``). Every ``/api/v1`` router except health requires an
authenticated caller allowed the ``read`` action. Endpoints that change state also require their
own action; which roles may perform each action is the policy's business (POLICY_ROLES for
``static-rbac``). The full route x role table is generated into docs/security/authz-matrix.md
(``scripts/authz_matrix.py``) and a test keeps it current.

Rate limits (api/ratelimit.py): an authenticated caller is charged one token per request, and a
client address one token per failed authentication; either bucket running dry answers 429.

With ``auth_enabled=False`` (local development and tests) every caller is an ADMIN named
"anonymous" and no policy check runs."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts

from oran_adapt.api.ratelimit import refuse
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import AuthenticationError, ConfigurationError
from oran_adapt.ports import Principal

READ = "read"
SUBMIT = "submit"
DATA = "data"
PROMOTE = "promote"
ADMIN = "admin"
PUBLIC = "public"

# Routes FastAPI itself adds for the interactive API docs (off in production by default).
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_POLICY = "__oran_policy__"
_UNLESS = "__oran_unless__"


def _peer(request: Request) -> str | None:
    return request.client.host if request.client else None


def authenticate(request: Request) -> Principal:
    """The caller behind this request. Raises AuthenticationError (401) when no valid credential
    was sent, RateLimitedError (429) when this address failed too often or this caller sent too
    many requests. The result is cached on ``request.state.principal``."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    state = request.app.state
    peer = _peer(request)
    if not state.settings.auth_enabled:
        principal = Principal(name="anonymous", role=Role.ADMIN)
        key = f"peer:{peer}"
    else:
        failures = state.auth_failures
        wait = failures.retry_after(f"peer:{peer}")
        if wait > 0:
            raise refuse(wait, "failed authentications from this address")
        try:
            principal = state.auth.authenticate(request.headers, peer=peer)
        except AuthenticationError:
            failures.take(f"peer:{peer}")
            raise
        key = f"principal:{principal.name}"
    wait = state.rate_limits.take(key)
    if wait > 0:
        raise refuse(wait, "requests")
    request.state.principal = principal
    return principal


def public(request: Request) -> None:
    """Marks a route as open to anyone (liveness, readiness). Deliberately explicit: a route
    with no policy at all refuses to start."""
    del request  # nothing to check: the marker is the point


setattr(public, _POLICY, PUBLIC)


def require(action: str, *, unless: str | None = None) -> Callable[[Request], Principal | None]:
    """A FastAPI dependency admitting only callers the policy allows ``action`` (403
    otherwise). ``unless`` names a boolean setting that, when true, makes the route public
    (METRICS_PUBLIC for /metrics)."""

    def _check(request: Request) -> Principal | None:
        if unless is not None and getattr(request.app.state.settings, unless):
            return None
        principal = authenticate(request)
        if request.app.state.settings.auth_enabled:
            request.app.state.policy.authorize(principal, action)
        return principal

    setattr(_check, _POLICY, action)
    setattr(_check, _UNLESS, unless)
    return _check


# Endpoint parameter types for callers that also need to know who is calling (the audit actor).
Submitter = Annotated[Principal, Depends(require(SUBMIT))]
DataEditor = Annotated[Principal, Depends(require(DATA))]
Promoter = Annotated[Principal, Depends(require(PROMOTE))]
Admin = Annotated[Principal, Depends(require(ADMIN))]


# ---- route policies ----------------------------------------------------------------------------
@dataclass(frozen=True)
class RoutePolicy:
    path: str
    methods: tuple[str, ...]
    policy: tuple[str, ...]  # actions, or (PUBLIC,); empty when the route declares none
    unless: str | None = None


def _markers(dependant: Any) -> Iterator[Callable[..., Any]]:
    for dep in dependant.dependencies:
        if hasattr(dep.call, _POLICY):
            yield dep.call
        yield from _markers(dep)


def route_policies(app: FastAPI) -> list[RoutePolicy]:
    """Every route of ``app`` with the policy it declares."""
    found: list[RoutePolicy] = []
    # iter_route_contexts expands included routers into their routes as served (prefix and
    # router-level dependencies applied).
    for route in iter_route_contexts(app.routes):
        path = route.path or ""
        methods = tuple(sorted(route.methods or ()))
        if isinstance(route.original_route, APIRoute):
            markers = list(_markers(route.dependant))
            actions = tuple(dict.fromkeys(getattr(m, _POLICY) for m in markers))
            unless = next((getattr(m, _UNLESS, None) for m in markers
                           if getattr(m, _UNLESS, None)), None)
            if PUBLIC in actions and len(actions) > 1:
                actions = tuple(a for a in actions if a != PUBLIC)
            found.append(RoutePolicy(path, methods, actions, unless))
        else:
            found.append(RoutePolicy(path, methods, (PUBLIC,) if path in DOCS_PATHS else ()))
    return found


def enforce_route_policies(app: FastAPI) -> None:
    """Refuse to serve an app with a route that declares no policy (deny by default)."""
    missing = [f"{' '.join(p.methods) or 'ANY'} {p.path}" for p in route_policies(app)
               if not p.policy]
    if missing:
        raise ConfigurationError(
            "every route must declare a policy (Depends(public) or Depends(require(...)))",
            routes=missing,
        )


def authz_matrix_markdown(app: FastAPI, policy_roles: Mapping[str, list[str]]) -> str:
    """The route x role table for docs/security/authz-matrix.md, from the routes' declared
    policies and POLICY_ROLES."""
    roles = list(Role)
    head = "| Method | Path | Actions | " + " | ".join(r.value for r in roles) + " |"
    rows = [head, "|" + "---|" * (3 + len(roles))]
    for p in sorted(route_policies(app), key=lambda p: (p.path, p.methods)):
        if p.path in DOCS_PATHS:
            continue
        for method in p.methods:
            if p.policy == (PUBLIC,):
                actions, cells = "public", ["yes"] * len(roles)
            else:
                actions = ", ".join(p.policy)
                if p.unless:
                    actions += f" (public when {p.unless.upper()})"
                cells = ["yes" if all(r.value in policy_roles.get(a, []) for a in p.policy)
                         else "-" for r in roles]
            rows.append(f"| {method} | `{p.path}` | {actions} | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"
