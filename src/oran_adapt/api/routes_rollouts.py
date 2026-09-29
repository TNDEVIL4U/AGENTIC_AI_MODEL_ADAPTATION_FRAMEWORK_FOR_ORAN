"""Progressive delivery API (delivery.controller, docs/adapters/rollout_metrics.md).

    GET  /rollouts                        ``read``; newest first, filterable
    GET  /rollouts/{id}                   ``read``; one rollout with its full history
    POST /rollouts/{id}/approve           ``promote``: approve a rollout AWAITING_APPROVAL
                                          (409 in any other state, or once it expired)
    POST /rollouts/{id}/reject            ``promote``: stop an active rollout; any traffic
                                          split is removed and read back first
    POST /rollouts/{id}/observations      ``submit``: one observation of an arm's online
                                          metrics (the ``api`` rollout metrics source)
"""

from __future__ import annotations

import math
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, field_validator

from oran_adapt.api.security import Promoter, Submitter
from oran_adapt.db.base import session_scope
from oran_adapt.delivery import controller
from oran_adapt.delivery.controller import Delivery

router = APIRouter(prefix="/rollouts", tags=["rollouts"])

State = Literal["SHADOW", "CANARY", "AB", "AWAITING_APPROVAL", "PROMOTED", "ROLLED_BACK",
                "EXPIRED", "REJECTED"]


class Decision(BaseModel):
    reason: str = Field(default="", max_length=2000)


class Observation(BaseModel):
    arm: Literal["stable", "candidate"]
    requests: int = Field(default=1, ge=1)
    metrics: dict[str, float] = Field(min_length=1, max_length=50)

    @field_validator("metrics")
    @classmethod
    def _finite(cls, value: dict[str, float]) -> dict[str, float]:
        bad = [k for k, v in value.items() if not math.isfinite(v) or not 0 < len(k) <= 100]
        if bad:
            raise ValueError(f"metric values must be finite and names 1-100 characters: {bad}")
        return value


def _delivery(request: Request) -> Delivery:
    state = request.app.state
    return Delivery.from_settings(
        state.settings, state.registry, deployer=state.deployer, source=state.rollout_metrics
    )


@router.get("")
def list_rollouts(
    request: Request,
    model_id: str | None = Query(default=None, max_length=200),
    state: State | None = None,
    active: bool | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    settings = request.app.state.settings
    limit = limit or settings.api_pagination_default_limit
    with session_scope(request.app.state.session_factory) as session:
        rows = controller.list_rollouts(session, model_id=model_id, state=state, active=active,
                                        limit=limit, offset=offset)
        return {"items": [controller.to_dict(r) for r in rows], "limit": limit,
                "offset": offset}


@router.get("/{rollout_id}")
def get_rollout(request: Request, rollout_id: str) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        return controller.to_dict(controller.get_rollout(session, rollout_id))


@router.post("/{rollout_id}/approve")
def approve(request: Request, rollout_id: str, body: Decision,
            principal: Promoter) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        row = controller.approve(session, _delivery(request), rollout_id,
                                 actor=principal.name, reason=body.reason)
        return controller.to_dict(row)


@router.post("/{rollout_id}/reject")
def reject(request: Request, rollout_id: str, body: Decision,
           principal: Promoter) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        row = controller.reject(session, _delivery(request), rollout_id,
                                actor=principal.name, reason=body.reason)
        return controller.to_dict(row)


@router.post("/{rollout_id}/observations", status_code=201)
def observe(request: Request, rollout_id: str, body: Observation,
            principal: Submitter) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        row = controller.observe(session, rollout_id, arm=body.arm, requests=body.requests,
                                 metrics_=body.metrics)
        return {"id": row.id, "rollout_id": rollout_id, "arm": row.arm,
                "requests": row.requests, "observed_at": row.observed_at.isoformat()}
