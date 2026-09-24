"""Where CDC events come from.

* ``KafkaCdcSource`` (CDC_MODE=kafka, production): Debezium streams PostgreSQL's WAL for the
  kpi_sample table to a Kafka topic; this reads it as a consumer group member with auto-commit
  off. Kafka offsets are committed only after the events are safely in the database, so a crash
  in between redelivers them, and cdc.store drops the duplicates.
* ``PollingCdcSource`` (CDC_MODE=polling, local fallback): the migration 0005 triggers write
  every INSERT/UPDATE/DELETE on kpi_sample to cdc_changelog; this reads it past the offset
  stored in cdc_offset (moved in the same transaction as the events).

Both return (events, position); the consumer stores them and then calls ``ack()``.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.cdc.events import CdcEvent, from_changelog_row, from_debezium
from oran_adapt.cdc.store import get_offset
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import CdcUnavailableError
from oran_adapt.db.models import CdcChangelog


class CdcSource(Protocol):
    name: str

    def fetch(self, session: Session, limit: int) -> tuple[list[CdcEvent], str | None]: ...

    def ack(self) -> None: ...

    def close(self) -> None: ...


class PollingCdcSource:
    def __init__(self, table: str = "kpi_sample") -> None:
        self.table = table
        self.name = f"polling:{table}"

    def fetch(self, session: Session, limit: int) -> tuple[list[CdcEvent], str | None]:
        after = int(get_offset(session, self.name) or 0)
        rows = session.execute(
            select(CdcChangelog)
            .where(CdcChangelog.table_name == self.table, CdcChangelog.seq > after)
            .order_by(CdcChangelog.seq)
            .limit(limit)
        ).scalars().all()
        events = [
            from_changelog_row(
                r.seq, r.table_name, r.operation, r.pk, r.old_row, r.new_row, r.tx_id,
                r.changed_at,
            )
            for r in rows
        ]
        return events, (str(rows[-1].seq) if rows else None)

    def ack(self) -> None:  # the offset is committed with the events
        return None

    def close(self) -> None:
        return None


def _kafka_consumer(settings: Settings) -> Any:
    try:
        from confluent_kafka import Consumer  # optional dependency: pip install oran-adapt[kafka]
    except ImportError as exc:
        raise CdcUnavailableError(
            "CDC_MODE=kafka needs the confluent-kafka package (install the 'kafka' extra)"
        ) from exc
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.cdc_consumer_group,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


class KafkaCdcSource:
    """``consumer`` is anything with confluent_kafka.Consumer's subscribe/consume/commit/close
    (tests pass a fake); by default a real Consumer is built from the settings."""

    def __init__(self, settings: Settings, consumer: Any | None = None) -> None:
        self.topic = settings.cdc_kafka_topic
        self.timeout = settings.cdc_kafka_poll_timeout_s
        self.name = f"kafka:{settings.cdc_consumer_group}:{self.topic}"
        self._consumer = consumer if consumer is not None else _kafka_consumer(settings)
        self._consumer.subscribe([self.topic])
        self._uncommitted = False

    def fetch(self, session: Session, limit: int) -> tuple[list[CdcEvent], str | None]:
        try:
            messages = self._consumer.consume(num_messages=limit, timeout=self.timeout)
        except Exception as exc:  # KafkaException; the client library is optional
            raise CdcUnavailableError(f"Kafka consume failed: {exc}", topic=self.topic) from exc
        events: list[CdcEvent] = []
        offsets: dict[str, int] = {}
        for msg in messages:
            err = msg.error()
            if err is not None:
                # Includes broker-down / transport errors. Nothing is stored or committed, so
                # the batch is read again once Kafka is back.
                raise CdcUnavailableError(f"Kafka error: {err}", topic=self.topic)
            event = from_debezium(
                msg.value(), topic=msg.topic(), partition=msg.partition(), offset=msg.offset()
            )
            if event is not None:
                events.append(event)
            offsets[str(msg.partition())] = msg.offset()
        self._uncommitted = bool(messages)
        return events, (json.dumps(offsets, sort_keys=True) if offsets else None)

    def ack(self) -> None:
        if self._uncommitted:
            self._consumer.commit(asynchronous=False)
            self._uncommitted = False

    def close(self) -> None:
        self._consumer.close()


def build_source(settings: Settings, *, kafka_consumer: Any | None = None) -> CdcSource:
    if settings.cdc_mode == "kafka":
        return KafkaCdcSource(settings, kafka_consumer)
    if settings.cdc_mode == "polling":
        return PollingCdcSource()
    raise CdcUnavailableError("CDC is disabled (set CDC_MODE=polling or CDC_MODE=kafka)")
