"""The CDC consumer loop: fetch a batch from the configured source, store it (deduplicated) with
the new offset in one transaction, then acknowledge it to the source.

Failure handling: if the source is unreachable (CdcUnavailableError) nothing is stored or
acknowledged. If storing fails the transaction rolls back and the batch is not acknowledged
(CdcProcessingError), so it is fetched again next time and duplicates are skipped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from oran_adapt.cdc.sources import CdcSource, build_source
from oran_adapt.cdc.store import store_events
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import CdcProcessingError
from oran_adapt.core.logging import log_event

logger = logging.getLogger(__name__)


def run_cdc_once(
    session_factory: Callable[[], Session], settings: Settings, *, source: CdcSource | None = None
) -> dict:
    """Process one batch. Returns counts; raises CdcUnavailableError / CdcProcessingError."""
    source = source or build_source(settings)
    with session_factory() as session:
        events, position = source.fetch(session, settings.cdc_batch_size)
        try:
            result = store_events(session, events, consumer=source.name, position=position)
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise CdcProcessingError(
                "could not store CDC batch; it will be redelivered",
                consumer=source.name, events=len(events),
            ) from exc
    result.count_metrics()
    source.ack()
    summary = {
        "consumer": source.name,
        "fetched": len(events),
        "stored": result.stored,
        "duplicates": result.duplicates,
        "operations": result.operations,
        "position": position,
    }
    if events:
        log_event(logger, "cdc batch stored", component="cdc", **summary)
    return summary


def run_cdc(
    session_factory: Callable[[], Session],
    settings: Settings,
    *,
    source: CdcSource | None = None,
    idle_sleep_s: float = 1.0,
    max_batches: int | None = None,
) -> dict:
    """Keep consuming until interrupted (or ``max_batches``), sleeping when a batch is empty."""
    source = source or build_source(settings)
    totals = {"batches": 0, "stored": 0, "duplicates": 0}
    try:
        while max_batches is None or totals["batches"] < max_batches:
            summary = run_cdc_once(session_factory, settings, source=source)
            totals["batches"] += 1
            totals["stored"] += summary["stored"]
            totals["duplicates"] += summary["duplicates"]
            if not summary["fetched"]:
                time.sleep(idle_sleep_s)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return totals
