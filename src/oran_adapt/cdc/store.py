"""Storing CDC events exactly once.

Each accepted event becomes one cdc_event row; its ``event_id`` is unique, so an event seen
again (a Kafka redelivery, a poller restarted before its offset was saved) is skipped. The
consumer's offset is written in the same transaction as the events, so the two never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.cdc.events import CdcEvent
from oran_adapt.core import metrics
from oran_adapt.db.models import CdcEventRecord, CdcOffset


@dataclass
class StoreResult:
    stored: int = 0
    duplicates: int = 0
    operations: dict[str, int] = field(default_factory=dict)
    accepted: list[CdcEvent] = field(default_factory=list)

    def count_metrics(self) -> None:
        """Call after the commit: only events that were really stored are counted."""
        now = datetime.now(UTC)
        for ev in self.accepted:
            metrics.CDC_EVENTS.labels(source=ev.source, operation=ev.operation.value).inc()
        if self.accepted:
            newest = max(ev.timestamp for ev in self.accepted)
            metrics.CDC_PROCESSING_LAG.set(max(0.0, (now - newest).total_seconds()))


def get_offset(session: Session, consumer: str) -> str | None:
    row = session.get(CdcOffset, consumer)
    return row.position if row else None


def set_offset(session: Session, consumer: str, position: str) -> None:
    row = session.get(CdcOffset, consumer)
    if row is None:
        session.add(CdcOffset(consumer=consumer, position=position))
    else:
        row.position = position


def store_events(
    session: Session, events: list[CdcEvent], *, consumer: str, position: str | None
) -> StoreResult:
    """Add the events not already stored and move ``consumer``'s offset to ``position``. The
    caller commits, then calls ``count_metrics()`` on the result."""
    result = StoreResult()
    ids = [e.event_id for e in events]
    known = set(
        session.execute(
            select(CdcEventRecord.event_id).where(CdcEventRecord.event_id.in_(ids))
        ).scalars()
    ) if ids else set()
    for ev in events:
        if ev.event_id in known:
            result.duplicates += 1
            continue
        known.add(ev.event_id)  # a duplicate inside the same batch
        session.add(
            CdcEventRecord(
                event_id=ev.event_id,
                source=ev.source,
                source_table=ev.source_table,
                operation=ev.operation.value,
                primary_key=ev.primary_key,
                dataset_id=ev.dataset_id,
                old_value=ev.old_value,
                new_value=ev.new_value,
                event_ts=ev.timestamp,
                transaction_id=ev.transaction_id,
                source_offset=ev.source_offset,
                schema_version=ev.schema_version,
            )
        )
        result.stored += 1
        result.operations[ev.operation.value] = result.operations.get(ev.operation.value, 0) + 1
        result.accepted.append(ev)
    if position is not None:
        set_offset(session, consumer, position)
    session.flush()
    return result
