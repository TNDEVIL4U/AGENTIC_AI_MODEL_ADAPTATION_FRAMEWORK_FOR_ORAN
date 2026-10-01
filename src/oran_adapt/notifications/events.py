"""Writing events to the outbox.

``record_event`` adds a NotificationEvent and one PENDING NotificationDelivery per configured
sink to the caller's session. It never commits: the event is part of the caller's transaction,
so it is stored exactly when the change it reports is (a rolled-back transition leaves no
event, a committed one cannot lose its event). The dispatcher delivers it afterwards.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from oran_adapt.core import metrics
from oran_adapt.core.config import selected_adapters
from oran_adapt.db.models import NotificationDelivery, NotificationEvent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from oran_adapt.core.config import Settings

PENDING = "PENDING"
DELIVERED = "DELIVERED"
DEAD = "DEAD"
STATUSES = (PENDING, DELIVERED, DEAD)
SPEC_VERSION = "1.0"
CONTENT_TYPE = "application/json"


def job_event_type(status: str) -> str:
    """The event type of a job entering ``status``: ``job.completed``, ``job.failed``..."""
    return f"job.{status.lower()}"


def sinks_for(settings: Settings, event_type: str) -> list[str]:
    """The configured sinks that receive ``event_type`` (NOTIFICATION_SINK_EVENTS)."""
    return [
        sink
        for sink in selected_adapters(settings, "notification_backend")
        if sink not in settings.notification_sink_events
        or event_type in settings.notification_sink_events[sink]
    ]


def body_of(envelope: dict[str, Any]) -> bytes:
    """The canonical serialization of an envelope: what is sent and signed."""
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode()


def record_event(
    session: Session,
    settings: Settings,
    *,
    event_type: str,
    subject: str,
    data: dict[str, Any],
    model_id: str | None = None,
) -> str:
    """Stage one event and its deliveries in ``session``; returns the event id."""
    now = datetime.now(UTC)
    event_id = uuid.uuid4().hex
    envelope = {
        "specversion": SPEC_VERSION,
        "id": event_id,
        "source": settings.notification_source,
        "type": settings.notification_event_type_prefix + event_type,
        "subject": subject,
        "time": now.isoformat(),
        "datacontenttype": CONTENT_TYPE,
        "data": json.loads(json.dumps(data, default=str)),
    }
    event = NotificationEvent(
        event_id=event_id,
        event_type=event_type,
        subject=subject,
        model_id=model_id,
        envelope=envelope,
        created_at=now,
    )
    session.add(event)
    for sink in sinks_for(settings, event_type):
        session.add(
            NotificationDelivery(
                event=event,
                sink=sink,
                status=PENDING,
                attempts=0,
                redrive_count=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    metrics.NOTIFICATION_EVENTS.labels(event_type).inc()
    return event_id


def record_job_transition(
    session: Session,
    settings: Settings,
    *,
    job_id: str,
    model_id: str | None,
    from_status: str | None,
    to_status: str,
    message: str = "",
    error: dict[str, Any] | None = None,
    strategy: str | None = None,
) -> str:
    """The event for one job state transition (type ``job.<to_status>``)."""
    return record_event(
        session,
        settings,
        event_type=job_event_type(to_status),
        subject=job_id,
        model_id=model_id,
        data={
            "job_id": job_id,
            "model_id": model_id,
            "from_status": from_status,
            "to_status": to_status,
            "message": message,
            "strategy": strategy,
            "error": error,
        },
    )
