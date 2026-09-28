"""API authentication and authorization, through the AuthPort and PolicyPort adapters that
AUTH_BACKEND and POLICY_BACKEND select (oran_adapt.bootstrap; ``app.state.auth`` and
``app.state.policy``).

Every ``/api/v1`` router except health requires an authenticated caller allowed the ``read``
action. Endpoints that change state also require their own action; which roles may perform each
action is the policy's business (POLICY_ROLES for ``static-rbac``):

    POST /adaptation/events                                       submit
    POST /datasets, /datasets/{id}/versions, /models/attach,
         /datasets/{id}/cdc/materialize                           data
    POST /models/{id}/rollback                                    promote (moves LIVE)

With ``auth_enabled=False`` (local development and tests) every caller is an ADMIN named
"anonymous" and no policy check runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from oran_adapt.core.enums import Role
from oran_adapt.ports import Principal

READ = "read"
SUBMIT = "submit"
DATA = "data"
PROMOTE = "promote"
ADMIN = "admin"


def authenticate(request: Request) -> Principal:
    """The caller behind this request. Raises AuthenticationError (401) when no valid credential
    was sent. The result is cached on ``request.state.principal``."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    if not request.app.state.settings.auth_enabled:
        principal = Principal(name="anonymous", role=Role.ADMIN)
    else:
        principal = request.app.state.auth.authenticate(request.headers)
    request.state.principal = principal
    return principal


def require(action: str) -> Callable[[Request], Principal]:
    """A FastAPI dependency admitting only callers the policy allows ``action`` (403
    otherwise)."""

    def _check(request: Request) -> Principal:
        principal = authenticate(request)
        if request.app.state.settings.auth_enabled:
            request.app.state.policy.authorize(principal, action)
        return principal

    return _check


# Endpoint parameter types for callers that also need to know who is calling (the audit actor).
Submitter = Annotated[Principal, Depends(require(SUBMIT))]
DataEditor = Annotated[Principal, Depends(require(DATA))]
Promoter = Annotated[Principal, Depends(require(PROMOTE))]
