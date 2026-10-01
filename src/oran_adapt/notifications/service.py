"""Reading the outbox and redriving dead deliveries (GET /api/v1/deliveries, the CLI).

Redrive puts a DEAD delivery back to PENDING with a fresh attempt budget, due now; the event
and its id are unchanged, so receivers still deduplicate it. Each redrive is audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from oran_adapt.core.audit import record_audit
from oran_adapt.core.enums import AuditAction
from oran_adapt.core.errors import ConflictError, DeliveryNotFoundError
from oran_adapt.db.models import NotificationDelivery, NotificationEvent
from oran_adapt.notifications.events import DEAD, PENDING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from sqlalchemy.sql import Select

COMPONENT = "notifications"


def _as_dict(d: NotificationDelivery, e: NotificationEvent, *, envelope: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": d.id,
        "event_id": d.event_id,
        "event_type": e.event_type,
        "subject": e.subject,
        "model_id": e.model_id,
        "sink": d.sink,
        "status": d.status,
        "attempts": d.attempts,
        "next_attempt_at": d.next_attempt_at,
        "in_flight": d.leased_by is not None,
        "last_error": d.last_error,
        "last_status_code": d.last_status_code,
        "redrive_count": d.redrive_count,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "delivered_at": d.delivered_at,
        "dead_at": d.dead_at,
    }
    if envelope:
        out["envelope"] = e.envelope
    return out


def _filtered(
    query: Select[Any], *, status: str | None, sink: str | None, event_type: str | None,
    subject: str | None,
) -> Select[Any]:
    if status is not None:
        query = query.where(NotificationDelivery.status == status)
    if sink is not None:
        query = query.where(NotificationDelivery.sink == sink)
    if event_type is not None:
        query = query.where(NotificationEvent.event_type == event_type)
    if subject is not None:
        query = query.where(NotificationEvent.subject == subject)
    return query


def list_deliveries(
    session: Session,
    *,
    limit: int,
    offset: int = 0,
    status: str | None = None,
    sink: str | None = None,
    event_type: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """A page of deliveries, newest first, with the total matching the filters."""
    filters = {"status": status, "sink": sink, "event_type": event_type, "subject": subject}
    joined = select(NotificationDelivery, NotificationEvent).join(
        NotificationEvent, NotificationEvent.event_id == NotificationDelivery.event_id
    )
    rows = session.execute(
        _filtered(joined, **filters)
        .order_by(NotificationDelivery.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = session.scalar(
        _filtered(
            select(func.count(NotificationDelivery.id)).join(
                NotificationEvent, NotificationEvent.event_id == NotificationDelivery.event_id
            ),
            **filters,
        )
    )
    return {
        "items": [_as_dict(d, e, envelope=False) for d, e in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


def _load(session: Session, delivery_id: int) -> tuple[NotificationDelivery, NotificationEvent]:
    row = session.execute(
        select(NotificationDelivery, NotificationEvent)
        .join(NotificationEvent, NotificationEvent.event_id == NotificationDelivery.event_id)
        .where(NotificationDelivery.id == delivery_id)
    ).one_or_none()
    if row is None:
        raise DeliveryNotFoundError(
            f"delivery {delivery_id} not found", delivery_id=delivery_id
        )
    return row[0], row[1]


def get_delivery(session: Session, delivery_id: int) -> dict[str, Any]:
    """One delivery with its event's envelope."""
    d, e = _load(session, delivery_id)
    return _as_dict(d, e, envelope=True)


def _redrive_rows(session: Session, ids: list[int], actor: str) -> int:
    now = datetime.now(UTC)
    moved = 0
    for delivery_id in ids:
        result = session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id, NotificationDelivery.status == DEAD)
            .values(status=PENDING, attempts=0, next_attempt_at=now, lease_until=None,
                    leased_by=None, dead_at=None, updated_at=now,
                    redrive_count=NotificationDelivery.redrive_count + 1)
        )
        moved += int(getattr(result, "rowcount", 0) == 1)
    if moved:
        record_audit(
            session, AuditAction.NOTIFICATION_REDRIVEN, component=COMPONENT, actor=actor,
            metadata={"delivery_ids": ids[:100], "count": moved},
        )
    return moved


def redrive(session: Session, delivery_id: int, *, actor: str) -> dict[str, Any]:
    """Re-queue one DEAD delivery; ConflictError if it is not dead."""
    d, _ = _load(session, delivery_id)
    if d.status != DEAD:
        raise ConflictError(
            f"delivery {delivery_id} is {d.status}; only DEAD deliveries can be redriven",
            delivery_id=delivery_id, status=d.status,
        )
    if _redrive_rows(session, [delivery_id], actor) != 1:
        raise ConflictError(
            f"delivery {delivery_id} changed while being redriven", delivery_id=delivery_id
        )
    session.expire_all()
    return get_delivery(session, delivery_id)


def redrive_dead(
    session: Session,
    *,
    actor: str,
    limit: int,
    sink: str | None = None,
    event_type: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Re-queue up to ``limit`` DEAD deliveries matching the filters, oldest first."""
    ids = list(
        session.scalars(
            _filtered(
                select(NotificationDelivery.id).join(
                    NotificationEvent,
                    NotificationEvent.event_id == NotificationDelivery.event_id,
                ),
                status=DEAD, sink=sink, event_type=event_type, subject=subject,
            )
            .order_by(NotificationDelivery.id)
            .limit(limit)
        )
    )
    return {"redriven": _redrive_rows(session, ids, actor), "ids": ids}
