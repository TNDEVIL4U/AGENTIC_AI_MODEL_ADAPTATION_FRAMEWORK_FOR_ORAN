"""CDC source adapter ``kafka`` (production): Debezium streams PostgreSQL's WAL for the source
table to a Kafka topic; this reads it as a consumer group member with auto-commit off. Offsets
are committed only after the events are safely in the database (``ack``), so a crash in between
redelivers them and cdc.store drops the duplicates."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from oran_adapt.cdc.events import CdcEvent, CdcRowMapping, from_debezium
from oran_adapt.core.errors import CdcUnavailableError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from oran_adapt.core.config import Settings


def _topic_not_created(err: Any) -> bool:
    name = getattr(err, "name", None)
    return callable(name) and name() == "UNKNOWN_TOPIC_OR_PART"


def _kafka_consumer(settings: Settings) -> Any:
    # Checked before the client exists: librdkafka aborts the whole process (an assertion in
    # Consumer_init) on a None group.id, and settings copied with model_copy skip validation.
    missing = [key.upper() for key in ("kafka_bootstrap_servers", "cdc_kafka_topic",
                                       "cdc_consumer_group") if not getattr(settings, key)]
    if missing:
        raise CdcUnavailableError(f"CDC_MODE=kafka requires {', '.join(missing)}",
                                  missing=missing)
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
            "auto.offset.reset": settings.cdc_kafka_auto_offset_reset,
        }
    )


class KafkaCdcSource:
    """``consumer`` is anything with confluent_kafka.Consumer's subscribe/consume/commit/close;
    the factory builds a real Consumer from the settings."""

    def __init__(self, settings: Settings, consumer: Any) -> None:
        if not settings.cdc_kafka_topic or not settings.cdc_consumer_group:
            raise CdcUnavailableError(
                "CDC_MODE=kafka requires CDC_KAFKA_TOPIC and CDC_CONSUMER_GROUP")
        self.topic: str = settings.cdc_kafka_topic
        self.schema_ref = settings.cdc_schema_ref
        self.mapping = CdcRowMapping.from_settings(settings)
        self.timeout = settings.cdc_kafka_poll_timeout_s
        self.name = f"kafka:{settings.cdc_consumer_group}:{self.topic}"
        self._consumer = consumer
        self._consumer.subscribe([self.topic])
        self._uncommitted = False

    def fetch(self, session: Session, limit: int) -> tuple[list[CdcEvent], str | None]:
        try:
            messages = self._consumer.consume(num_messages=limit, timeout=self.timeout)
        except Exception as exc:  # KafkaException; the client library is optional
            raise CdcUnavailableError(f"Kafka consume failed: {exc}", topic=self.topic) from exc
        if not any(m.error() is None for m in messages):
            self._probe()
        events: list[CdcEvent] = []
        offsets: dict[str, int] = {}
        for msg in messages:
            err = msg.error()
            if err is not None and _topic_not_created(err):
                # Debezium creates the topic with the table's first change; until then there
                # is simply nothing to read (the probe above still catches a dead broker).
                continue
            if err is not None:
                # Includes broker-down / transport errors. Nothing is stored or committed, so
                # the batch is read again once Kafka is back.
                raise CdcUnavailableError(f"Kafka error: {err}", topic=self.topic)
            event = from_debezium(
                msg.value(),
                topic=msg.topic(),
                partition=msg.partition(),
                offset=msg.offset(),
                schema_ref=self.schema_ref,
                mapping=self.mapping,
            )
            if event is not None:
                events.append(event)
            offsets[str(msg.partition())] = msg.offset()
        self._uncommitted = bool(offsets)
        return events, (json.dumps(offsets, sort_keys=True) if offsets else None)

    def _probe(self) -> None:
        """An empty batch is also what librdkafka returns while every broker is down: it
        reports that through callbacks, not as message errors. A metadata request tells "no new
        changes" from an outage."""
        try:
            self._consumer.list_topics(self.topic, timeout=self.timeout)
        except Exception as exc:  # KafkaException; the client library is optional
            raise CdcUnavailableError(f"Kafka unreachable: {exc}", topic=self.topic) from exc

    def ack(self) -> None:
        if self._uncommitted:
            self._consumer.commit(asynchronous=False)
            self._uncommitted = False

    def close(self) -> None:
        self._consumer.close()


def _build(settings: Settings) -> KafkaCdcSource:
    return KafkaCdcSource(settings, _kafka_consumer(settings))


SPEC = AdapterSpec(
    capability=Capability(
        port="cdc_source",
        adapter="kafka",
        description="Debezium change events from a Kafka topic, committed after storage",
        features=frozenset({"at_least_once", "external_offsets", "network"}),
        config_keys=(
            "kafka_bootstrap_servers",
            "cdc_kafka_topic",
            "cdc_consumer_group",
            "cdc_kafka_poll_timeout_s",
            "cdc_kafka_auto_offset_reset",
            "cdc_schema_ref",
            "cdc_key_column",
            "cdc_dataset_column",
            "cdc_dataset_id",
            "cdc_time_column",
            "cdc_payload_column",
        ),
        required_keys=("kafka_bootstrap_servers", "cdc_kafka_topic", "cdc_consumer_group"),
        distributions=("confluent-kafka",),
    ),
    factory=_build,
)
