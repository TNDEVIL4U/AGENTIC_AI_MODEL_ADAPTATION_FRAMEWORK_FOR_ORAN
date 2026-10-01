"""CDC source adapter ``polling`` (local, no broker): reads the cdc_changelog rows that the
migration 0005 triggers write for CDC_POLLING_TABLE, past the offset stored in cdc_offset."""

from __future__ import annotations

from typing import TYPE_CHECKING

from oran_adapt.cdc.events import CdcRowMapping
from oran_adapt.cdc.sources import PollingCdcSource
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def _build(settings: Settings) -> PollingCdcSource:
    return PollingCdcSource(settings.cdc_polling_table, settings.cdc_schema_ref,
                            CdcRowMapping.from_settings(settings))


SPEC = AdapterSpec(
    capability=Capability(
        port="cdc_source",
        adapter="polling",
        description="trigger-fed cdc_changelog table, offset committed with the events",
        features=frozenset({"exactly_once_offsets", "offline"}),
        config_keys=("cdc_polling_table", "cdc_schema_ref", "cdc_key_column",
                     "cdc_dataset_column", "cdc_dataset_id", "cdc_time_column",
                     "cdc_payload_column"),
    ),
    factory=_build,
)
