"""Phase J (Rule 15): CDC batches that cut a source transaction in two still store every change
exactly once, advance the offset monotonically, and materialize to the same data version as one
big batch."""

from __future__ import annotations

from sqlalchemy import func, select
from test_phase14_stage_c import (  # shared fixtures and seeding (same test directory)
    _kpi_changes,
    polling,
    session_factory,
)

from oran_adapt.cdc import materialize_cdc, run_cdc_once
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import CdcEventRecord, CdcOffset

__all__ = ["polling", "session_factory"]  # fixtures re-used from test_phase14_stage_c


def test_small_batches_split_a_transaction_without_loss_or_duplication(
    session_factory, polling
) -> None:
    # The first change set inserts 3 rows in one transaction; batches of 2 cut it in two.
    _kpi_changes(session_factory)
    small = polling.model_copy(update={"cdc_batch_size": 2})

    offsets = []
    fetched = []
    while True:
        summary = run_cdc_once(session_factory, small)
        if not summary["fetched"]:
            break
        fetched.append(summary["fetched"])
        with session_scope(session_factory) as s:
            offsets.append(int(s.get(CdcOffset, "polling:kpi_sample").position))

    assert fetched == [2, 2, 1]
    assert offsets == sorted(offsets) == [2, 4, 5]
    with session_scope(session_factory) as s:
        assert s.scalar(select(func.count()).select_from(CdcEventRecord)) == 5
        version = materialize_cdc(s, "kpi")
    # Same result as the single-batch materialization in test_phase14_stage_c.
    assert version is not None and version.row_count == 2
    assert version.cdc_range["event_count"] == 5
