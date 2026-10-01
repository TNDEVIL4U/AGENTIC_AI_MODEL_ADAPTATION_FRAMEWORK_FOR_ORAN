"""Hardening Phase 13: the conformance suites and outage scenarios against real services started
by testcontainers (PostgreSQL, Kafka). CI runs these in the ``containers`` job; they need a
Docker daemon and the ``containers`` extra, so a host without them skips with an owner and an
expiry (tests/skip_policy.py)."""

from __future__ import annotations

import itertools
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from oran_adapt.bootstrap import build_cdc_source
from oran_adapt.conformance import cdc_source as cdc_suite
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import DatabaseUnavailableError
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.health import check_database
from oran_adapt.db.migrate import upgrade_to_head
from oran_adapt.db.models import KpiSample

pytestmark = [
    pytest.mark.integration,
    pytest.mark.heavy,
    pytest.mark.skipif(shutil.which("docker") is None,
                       reason="testcontainers needs a Docker daemon [owner=TNDEVIL4U expires=2027-03-31]"),
]
T0 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres() -> Iterator[object]:
    containers = pytest.importorskip("testcontainers.postgres",
                                     reason="install the 'containers' extra [owner=TNDEVIL4U expires=2027-03-31]")
    with containers.PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg


@pytest.fixture
def pg_settings(postgres, settings) -> Settings:
    """The suite's settings (temporary MLflow store) pointed at the container."""
    settings = settings.model_copy(update={"database_url": postgres.get_connection_url()})
    upgrade_to_head(settings.database_url)
    return settings


def test_migrations_and_polling_cdc_conformance_on_postgres(pg_settings) -> None:
    session_factory = make_session_factory(create_db_engine(pg_settings.database_url))
    settings = pg_settings.model_copy(update={"cdc_mode": "polling"})
    ids = itertools.count(1)

    def insert(n: int) -> None:
        with session_scope(session_factory) as s:
            for _ in range(n):
                i = next(ids)
                s.add(KpiSample(id=i, dataset_id="kpi", observed_at=T0,
                                payload={"prb_util": 0.1 * i, "rsrp": -90.0, "label": 1}))

    ctx = cdc_suite.Context(session_factory=session_factory, emit=insert,
                            reopen=lambda: build_cdc_source(settings))
    assert cdc_suite.run(build_cdc_source(settings), ctx) == list(cdc_suite.CHECKS)


def test_kafka_cdc_conformance_against_a_real_broker(pg_settings) -> None:
    kafka = pytest.importorskip("testcontainers.kafka",
                                reason="install the 'containers' extra [owner=TNDEVIL4U expires=2027-03-31]")
    producer_module = pytest.importorskip("confluent_kafka",
                                          reason="install the 'kafka' extra [owner=TNDEVIL4U expires=2027-03-31]")
    session_factory = make_session_factory(create_db_engine(pg_settings.database_url))
    with kafka.KafkaContainer("confluentinc/cp-kafka:7.6.1") as broker:
        settings = pg_settings.model_copy(update={
            "cdc_mode": "kafka", "kafka_bootstrap_servers": broker.get_bootstrap_server(),
        })
        producer = producer_module.Producer({"bootstrap.servers":
                                             settings.kafka_bootstrap_servers})
        ids = itertools.count(10_000)

        def emit(n: int) -> None:
            for _ in range(n):
                i = next(ids)
                row = {"id": i, "dataset_id": "kpi", "observed_at": "2026-03-01T00:00:00Z",
                       "payload": json.dumps({"prb_util": 0.1, "rsrp": -90.0, "label": 1})}
                payload = {"before": None, "after": row, "op": "c", "ts_ms": 1_767_225_600_500,
                           "source": {"connector": "postgresql", "table": "kpi_sample",
                                      "txId": i, "lsn": i, "ts_ms": 1_767_225_600_000}}
                producer.produce(settings.cdc_kafka_topic,
                                 json.dumps({"schema": {}, "payload": payload}).encode())
            producer.flush(10)

        ctx = cdc_suite.Context(session_factory=session_factory, emit=emit,
                                reopen=lambda: build_cdc_source(settings),
                                break_source=broker.stop)
        assert cdc_suite.run(build_cdc_source(settings), ctx) == list(cdc_suite.CHECKS)


def test_postgres_going_away_is_a_typed_outage(postgres, pg_settings) -> None:
    engine = create_db_engine(pg_settings.database_url)
    check_database(engine)
    postgres.stop()
    with pytest.raises(DatabaseUnavailableError):
        check_database(create_db_engine(pg_settings.database_url))
