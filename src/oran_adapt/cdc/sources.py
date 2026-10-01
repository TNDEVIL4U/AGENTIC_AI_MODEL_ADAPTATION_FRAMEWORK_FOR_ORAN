"""Where CDC events come from: a CdcSourcePort adapter chosen by CDC_MODE at the composition root
(oran_adapt.bootstrap.build_cdc_source).

* ``kafka`` (oran_adapt.adapters.kafka_cdc, production): Debezium streams PostgreSQL's WAL for the
  source table to a Kafka topic, read as a consumer group member with auto-commit off. Offsets are
  committed only after the events are safely in the database, so a crash in between redelivers
  them, and cdc.store drops the duplicates.
* ``polling`` (``PollingCdcSource`` below, local fallback): the migration 0005 triggers write
  every INSERT/UPDATE/DELETE on the source table to cdc_changelog; this reads it past the offset
  stored in cdc_offset (moved in the same transaction as the events).

Both return (events, position); the consumer stores them and then calls ``ack()``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.cdc.events import DEFAULT_MAPPING, CdcEvent, CdcRowMapping, from_changelog_row
from oran_adapt.cdc.store import get_offset
from oran_adapt.db.models import CdcChangelog
from oran_adapt.ports import CdcSourcePort

CdcSource = CdcSourcePort


class PollingCdcSource:
    def __init__(
        self, table: str, schema_ref: str, mapping: CdcRowMapping = DEFAULT_MAPPING
    ) -> None:
        self.table = table
        self.schema_ref = schema_ref
        self.mapping = mapping
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
                r.changed_at, schema_ref=self.schema_ref, mapping=self.mapping,
            )
            for r in rows
        ]
        return events, (str(rows[-1].seq) if rows else None)

    def ack(self) -> None:  # the offset is committed with the events
        return None

    def close(self) -> None:
        return None
