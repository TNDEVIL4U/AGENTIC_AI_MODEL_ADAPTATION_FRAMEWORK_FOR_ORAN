"""Change data capture: a CdcSourcePort adapter chosen by Settings.cdc_mode (Debezium/Kafka in
production, a trigger changelog as the local fallback), idempotent event storage, and materialization into data versions."""

from oran_adapt.cdc.consumer import run_cdc, run_cdc_once
from oran_adapt.cdc.events import CdcEvent, from_changelog_row, from_debezium
from oran_adapt.cdc.materialize import materialize_cdc
from oran_adapt.cdc.sources import CdcSource, PollingCdcSource
from oran_adapt.cdc.store import store_events

__all__ = [
    "CdcEvent",
    "CdcSource",
    "PollingCdcSource",
    "from_changelog_row",
    "from_debezium",
    "materialize_cdc",
    "run_cdc",
    "run_cdc_once",
    "store_events",
]
