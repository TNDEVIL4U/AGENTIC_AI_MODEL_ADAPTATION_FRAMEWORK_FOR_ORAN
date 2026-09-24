"""The one CDC event shape both sources are parsed into, and the two parsers.

``event_id`` is derived from where the change sits in the source's own log (the WAL position
for Debezium, the changelog sequence for the polling fallback) plus table, key and operation.
A redelivered change therefore always has the same id, which is what makes processing
idempotent (see cdc.store).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel

from oran_adapt.core.enums import CdcOperation
from oran_adapt.core.errors import CdcProcessingError

# Version of the row image the parsers produce ({id, dataset_id, observed_at, payload}).
KPI_SAMPLE_SCHEMA_VERSION = "kpi_sample/1"
_DEBEZIUM_OPS = {
    "c": CdcOperation.INSERT,
    "r": CdcOperation.INSERT,  # snapshot read: the row exists at snapshot time
    "u": CdcOperation.UPDATE,
    "d": CdcOperation.DELETE,
}


class CdcEvent(BaseModel):
    event_id: str
    source: str  # "polling" | "debezium"
    source_table: str
    operation: CdcOperation
    primary_key: str
    old_value: dict | None = None
    new_value: dict | None = None
    timestamp: datetime  # when the source row changed
    transaction_id: str | None = None
    source_offset: str  # position in the source log (changelog seq, or topic:partition:offset)
    schema_version: str

    @property
    def dataset_id(self) -> str | None:
        row = self.new_value or self.old_value or {}
        return row.get("dataset_id")


def make_event_id(source: str, table: str, primary_key: str, op: str, position: str) -> str:
    raw = json.dumps([source, table, primary_key, op, position])
    return hashlib.sha256(raw.encode()).hexdigest()


def _utc(value: Any) -> datetime:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
    return ts.to_pydatetime()


def _normalize_row(row: dict | None) -> dict | None:
    """The row image in one shape whichever source it came from: JSON payload decoded,
    observed_at as ISO-8601 UTC."""
    if row is None:
        return None
    payload = row.get("payload")
    if isinstance(payload, str):  # Debezium's io.debezium.data.Json arrives as a string
        payload = json.loads(payload)
    observed = row.get("observed_at")
    if isinstance(observed, int):  # Debezium MicroTimestamp for a timestamp without zone
        observed = datetime.fromtimestamp(observed / 1_000_000, UTC)
    return {
        "id": row.get("id"),
        "dataset_id": row.get("dataset_id"),
        "observed_at": _utc(observed).isoformat() if observed is not None else None,
        "payload": payload or {},
    }


def from_changelog_row(
    seq: int, table: str, operation: str, pk: str, old_row: str | None, new_row: str | None,
    tx_id: str | None, changed_at: str,
) -> CdcEvent:
    """A cdc_changelog row (written by the migration 0005 triggers) as a CdcEvent."""
    op = CdcOperation(operation)
    return CdcEvent(
        event_id=make_event_id("polling", table, pk, op, str(seq)),
        source="polling",
        source_table=table,
        operation=op,
        primary_key=pk,
        old_value=_normalize_row(json.loads(old_row)) if old_row else None,
        new_value=_normalize_row(json.loads(new_row)) if new_row else None,
        timestamp=_utc(changed_at),
        transaction_id=tx_id,
        source_offset=str(seq),
        schema_version=KPI_SAMPLE_SCHEMA_VERSION,
    )


def from_debezium(
    value: bytes | str | dict | None, *, topic: str, partition: int, offset: int,
    key_field: str = "id",
) -> CdcEvent | None:
    """A Debezium change message (JSON converter, with or without the schema wrapper) as a
    CdcEvent. Returns None for tombstones (the null-value message Kafka compaction uses after a
    delete) and heartbeats. Raises CdcProcessingError for a message that is not a Debezium
    change envelope."""
    if value is None:
        return None
    if isinstance(value, bytes | str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise CdcProcessingError(
                "CDC message is not JSON", topic=topic, partition=partition, offset=offset
            ) from exc
    envelope = value.get("payload", value) if isinstance(value, dict) else None
    if not isinstance(envelope, dict) or "op" not in envelope:
        if isinstance(envelope, dict) and "ts_ms" in envelope and "source" not in envelope:
            return None  # heartbeat
        raise CdcProcessingError(
            "CDC message is not a Debezium change envelope",
            topic=topic, partition=partition, offset=offset,
        )
    op = _DEBEZIUM_OPS.get(envelope["op"])
    if op is None:  # "t" (truncate) and "m" (message) carry no row
        return None
    before, after = envelope.get("before"), envelope.get("after")
    source = envelope.get("source") or {}
    row = after or before or {}
    if key_field not in row:
        raise CdcProcessingError(
            f"CDC row has no primary key field {key_field!r}", topic=topic, offset=offset
        )
    pk = str(row[key_field])
    table = source.get("table") or topic.rsplit(".", 1)[-1]
    # The WAL position identifies the change even if it lands at a new Kafka offset (e.g. the
    # connector restarts and re-sends); fall back to the Kafka coordinates.
    position = str(source["lsn"]) if source.get("lsn") is not None else (
        f"{topic}:{partition}:{offset}"
    )
    ts_ms = source.get("ts_ms") or envelope.get("ts_ms")
    tx = source.get("txId")
    return CdcEvent(
        event_id=make_event_id("debezium", table, pk, op, position),
        source="debezium",
        source_table=table,
        operation=op,
        primary_key=pk,
        old_value=_normalize_row(before),
        new_value=_normalize_row(after),
        timestamp=datetime.fromtimestamp(ts_ms / 1000, UTC) if ts_ms else datetime.now(UTC),
        transaction_id=str(tx) if tx is not None else None,
        source_offset=f"{topic}:{partition}:{offset}",
        schema_version=f"{KPI_SAMPLE_SCHEMA_VERSION};debezium/{source.get('version', '?')}",
    )
