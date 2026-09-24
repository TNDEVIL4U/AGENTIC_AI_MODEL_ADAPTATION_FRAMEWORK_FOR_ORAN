"""Turning stored CDC events into immutable data versions.

All of a dataset's events not yet materialized are folded, in the order they were consumed,
into the net change per source row: the row's last image if it still exists, or a deletion.
That becomes one new CDC data version (kind CDC, parent = the dataset's previous CDC version):
rows keyed by the source primary key (``record_key``), deleted keys in the version metadata,
and the offsets and transactions it covers in ``cdc_range``/``source_tx``. The events are then
marked with that version, in the same transaction, so each event lands in exactly one version.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.enums import CdcOperation, DataKind
from oran_adapt.datastore.versioning import VersionInfo, get_or_create_dataset, ingest_version
from oran_adapt.db.models import CdcEventRecord, DataVersion

_MAX_TX_IDS = 1000


def _previous_cdc_version(session: Session, dataset_pk: int) -> DataVersion | None:
    return session.execute(
        select(DataVersion)
        .where(DataVersion.dataset_id == dataset_pk, DataVersion.kind == DataKind.CDC.value)
        .order_by(DataVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def materialize_cdc(session: Session, dataset_id: str, *, actor: str = "cdc") -> VersionInfo | None:
    """Fold ``dataset_id``'s pending CDC events into a new CDC data version. Returns None when
    there is nothing pending (so running it twice is harmless). The caller commits."""
    events = session.execute(
        select(CdcEventRecord)
        .where(CdcEventRecord.dataset_id == dataset_id, CdcEventRecord.data_version_id.is_(None))
        .order_by(CdcEventRecord.id)
    ).scalars().all()
    if not events:
        return None

    net: dict[str, dict | None] = {}
    for ev in events:
        net[ev.primary_key] = None if ev.operation == CdcOperation.DELETE else ev.new_value
    upserts = sorted(
        ((pk, row) for pk, row in net.items() if row is not None),
        key=lambda item: (item[1]["observed_at"], item[0]),
    )
    deleted = sorted(pk for pk, row in net.items() if row is None)

    frame = pd.DataFrame([row["payload"] for _, row in upserts])
    frame["__observed_at"] = [row["observed_at"] for _, row in upserts]

    dataset = get_or_create_dataset(session, dataset_id)
    parent = _previous_cdc_version(session, dataset.id)
    first, last = events[0], events[-1]
    info = ingest_version(
        session,
        dataset_id,
        f"cdc-{first.id:06d}-{last.id:06d}",
        frame,
        kind=DataKind.CDC,
        timestamp_column="__observed_at",
        parent_version=parent.version if parent else None,
        source=f"cdc:{first.source} ({len(events)} events, {len(upserts)} rows, "
        f"{len(deleted)} deleted)",
        actor=actor,
        record_keys=[pk for pk, _ in upserts],
        deleted_keys=deleted,
        cdc_range={
            "source": first.source,
            "source_table": first.source_table,
            "first_offset": first.source_offset,
            "last_offset": last.source_offset,
            "first_event_id": first.event_id,
            "last_event_id": last.event_id,
            "event_count": len(events),
            "changed_from": min(e.event_ts for e in events).isoformat(),
            "changed_to": max(e.event_ts for e in events).isoformat(),
        },
        source_tx=sorted({e.transaction_id for e in events if e.transaction_id})[:_MAX_TX_IDS],
    )
    for ev in events:
        ev.data_version_id = info.data_version_id
    session.flush()
    return info
