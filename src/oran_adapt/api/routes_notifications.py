"""Notification outbox API: what was sent where, what failed and why, and redriving what died
(docs/adapters/notification.md).

    GET  /deliveries                  every caller with ``read``; newest first, filterable
    GET  /deliveries/{id}             one delivery with its CloudEvents envelope
    POST /deliveries/{id}/redrive     ``admin``: re-queue one DEAD delivery (409 if not dead)
    POST /deliveries/redrive          ``admin``: re-queue the DEAD deliveries matching filters
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from oran_adapt.api.security import Admin
from oran_adapt.db.base import session_scope
from oran_adapt.notifications import service

router = APIRouter(prefix="/deliveries", tags=["notifications"])

Status = Literal["PENDING", "DELIVERED", "DEAD"]


class BulkRedrive(BaseModel):
    sink: str | None = Field(default=None, max_length=50)
    event_type: str | None = Field(default=None, max_length=100)
    subject: str | None = Field(default=None, max_length=200)
    # Omitted: API_PAGINATION_DEFAULT_LIMIT.
    limit: int | None = Field(default=None, ge=1, le=1000)


@router.get("")
def list_deliveries(
    request: Request,
    status: Status | None = None,
    sink: str | None = Query(default=None, max_length=50),
    event_type: str | None = Query(default=None, max_length=100),
    subject: str | None = Query(default=None, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    state = request.app.state
    with session_scope(state.session_factory) as session:
        return service.list_deliveries(
            session,
            limit=limit or state.settings.api_pagination_default_limit,
            offset=offset,
            status=status,
            sink=sink,
            event_type=event_type,
            subject=subject,
        )


@router.post("/redrive")
def redrive_dead(request: Request, body: BulkRedrive, principal: Admin) -> dict[str, Any]:
    state = request.app.state
    with session_scope(state.session_factory) as session:
        return service.redrive_dead(
            session,
            actor=principal.name,
            limit=body.limit or state.settings.api_pagination_default_limit,
            sink=body.sink,
            event_type=body.event_type,
            subject=body.subject,
        )


@router.get("/{delivery_id}")
def get_delivery(request: Request, delivery_id: int) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        return service.get_delivery(session, delivery_id)


@router.post("/{delivery_id}/redrive")
def redrive(request: Request, delivery_id: int, principal: Admin) -> dict[str, Any]:
    with session_scope(request.app.state.session_factory) as session:
        return service.redrive(session, delivery_id, actor=principal.name)
