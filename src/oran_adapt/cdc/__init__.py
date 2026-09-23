"""Change data capture: Debezium/Kafka in production, a trigger changelog as the local fallback
(Settings.cdc_mode), idempotent event storage, and materialization into data versions."""

from oran_adapt.cdc.consumer import run_cdc, run_cdc_once
from oran_adapt.cdc.events import CdcEvent, from_changelog_row, from_debezium
from oran_adapt.cdc.materialize import materialize_cdc
from oran_adapt.cdc.sources import KafkaCdcSource, PollingCdcSource, build_source
from oran_adapt.cdc.store import store_events

__all__ = [
    "CdcEvent",
    "KafkaCdcSource",
    "PollingCdcSource",
    "build_source",
    "from_changelog_row",
    "from_debezium",
    "materialize_cdc",
    "run_cdc",
    "run_cdc_once",
    "store_events",
]
