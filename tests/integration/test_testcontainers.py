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
            # No defaults since Hardening Phase 15, and model_copy skips the validator.
            "cdc_kafka_topic": "ci.public.kpi_sample", "cdc_consumer_group": "ci-cdc",
        })
        # In production Debezium creates the topic before anyone subscribes; a consumer that
        # subscribes to a missing topic is told UNKNOWN_TOPIC_OR_PART, an outage.
        kafka_admin = pytest.importorskip("confluent_kafka.admin", reason="install the 'containers' extra [owner=TNDEVIL4U expires=2027-03-31]")
        admin = kafka_admin.AdminClient({"bootstrap.servers":
                                                   settings.kafka_bootstrap_servers})
        for future in admin.create_topics(
            [kafka_admin.NewTopic(settings.cdc_kafka_topic, num_partitions=1,
                                  replication_factor=1)]).values():
            future.result(timeout=30)
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
                                break_source=lambda: broker.get_wrapped_container().stop())
        assert cdc_suite.run(build_cdc_source(settings), ctx) == list(cdc_suite.CHECKS)


def test_postgres_going_away_is_a_typed_outage() -> None:
    # Its own container: stopping the module's shared one made that fixture's teardown fail
    # removing a container that was already gone.
    containers = pytest.importorskip("testcontainers.postgres",
                                     reason="install the 'containers' extra [owner=TNDEVIL4U expires=2027-03-31]")
    with containers.PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        check_database(create_db_engine(url))
        pg.get_wrapped_container().stop()
        with pytest.raises(DatabaseUnavailableError):
            check_database(create_db_engine(url))


# ---- rankpi on real PostgreSQL --------------------------------------------------------------
# The OAI RAN KPI database's documented columns (schema.sql, schema_model.sql), with the derived
# relations as real views, so the PostgreSQL-only paths run: read-only transactions, the
# statement timeout, server-side streaming, view reflection and timestamptz values.
_RANKPI_DDL = (
    ("CREATE TABLE run (run_id serial PRIMARY KEY, label text, started_at timestamptz,"
     " ended_at timestamptz)"),
    "CREATE TABLE collector_totals (run_id int PRIMARY KEY, dropped bigint NOT NULL)",
    "CREATE VIEW v_run_summary AS SELECT run_id, dropped FROM collector_totals",
    ("CREATE TABLE slice_rate (ts timestamptz, run_id int, sst smallint, sd int,"
     " dl_thp_kbps numeric, prb_share numeric)"),
    ("CREATE VIEW v_slice_kpi AS SELECT ts, run_id, sst, sd, dl_thp_kbps, prb_share"
     " FROM slice_rate"),
    ("CREATE TABLE e2sm_mac_ue (ts timestamptz, run_id int, rnti int, wb_cqi int,"
     " dl_aggr_tbs bigint)"),
    ("CREATE TABLE ran_model (model_id serial PRIMARY KEY, name text UNIQUE, kind text"
     " CHECK (kind IN ('drl','bandit','heuristic','static','human','random')))"),
    ("CREATE TABLE ran_model_version (version_id serial PRIMARY KEY,"
     " model_id int REFERENCES ran_model, version text, status text"
     " CHECK (status IN ('dev','candidate','deployed','retired','failed')),"
     " UNIQUE (model_id, version))"),
    ("CREATE TABLE model_deployment (deployment_id serial PRIMARY KEY,"
     " version_id int REFERENCES ran_model_version, mode text"
     " CHECK (mode IN ('train','eval','shadow','deploy','replay')),"
     " started_at timestamptz, ended_at timestamptz)"),
    ("CREATE TABLE model_decision (decision_id serial PRIMARY KEY,"
     " deployment_id int REFERENCES model_deployment, step int, ts timestamptz,"
     " score numeric, inference_ms numeric, UNIQUE (deployment_id, step))"),
    ("CREATE TABLE score_term (decision_id int REFERENCES model_decision, name text,"
     " raw_value numeric, weight numeric, contribution numeric, PRIMARY KEY (decision_id, name))"),
    ("CREATE TABLE control_action (action_id serial PRIMARY KEY,"
     " decision_id int REFERENCES model_decision, raw_value numeric, requested_value numeric,"
     " applied_value numeric, clipped boolean)"),
    ("CREATE TABLE control_delivery (delivery_id serial PRIMARY KEY,"
     " decision_id int REFERENCES model_decision, outcome text"
     " CHECK (outcome IN ('sent','ack','nack','timeout','error','suppressed')))"),
    ("CREATE TABLE model_metric (metric_id serial PRIMARY KEY,"
     " deployment_id int REFERENCES model_deployment, name text, step int, ts timestamptz,"
     " value numeric, UNIQUE (deployment_id, name, step))"),
)


@pytest.fixture(scope="module")
def rankpi_url(postgres) -> str:
    from sqlalchemy import create_engine, text

    base = postgres.get_connection_url()
    with create_engine(base, isolation_level="AUTOCOMMIT").connect() as conn:
        conn.execute(text("CREATE DATABASE rankpi"))
    url = base.rsplit("/", 1)[0] + "/rankpi"
    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    with create_engine(url).begin() as conn:
        for ddl in _RANKPI_DDL:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO run (run_id, label, started_at, ended_at) VALUES"
                          " (5, 'closed', :t, :e), (6, 'open', :t, NULL), (7, 'lossy', :t, :e)"),
                     {"t": t0, "e": t0.replace(hour=13)})
        conn.execute(text("INSERT INTO collector_totals VALUES (5, 0), (6, 0), (7, 4)"))
        conn.execute(text("INSERT INTO slice_rate VALUES (:ts, :run, 1, :sd, :thp, 0.5)"),
                     [{"ts": t0.replace(second=s), "run": run, "sd": sd, "thp": 100 + s}
                      for run in (5, 6, 7) for s in range(10) for sd in (1, 2)])
        conn.execute(text("INSERT INTO e2sm_mac_ue VALUES (:ts, 5, 17, 12, :tbs)"),
                     [{"ts": t0.replace(second=s), "tbs": 1000 * s} for s in range(5)])
        conn.execute(text("INSERT INTO ran_model (name, kind) VALUES ('ppo_slice', 'drl')"))
        conn.execute(text("INSERT INTO ran_model_version (model_id, version, status) VALUES"
                          " (1, '3', 'candidate'), (1, '2', 'deployed')"))
        conn.execute(text("INSERT INTO model_deployment (version_id, mode, started_at) VALUES"
                          " (1, 'shadow', :t), (2, 'deploy', :t)"), {"t": t0})
        for i, (score, clipped, outcome) in enumerate(
                [(0.4, False, "ack"), (0.6, True, "timeout"), (0.5, False, "sent")]):
            conn.execute(text("INSERT INTO model_decision (deployment_id, step, ts, score,"
                              " inference_ms) VALUES (1, :s, :ts, :sc, 3.5)"),
                         {"s": i, "ts": t0.replace(second=i), "sc": score})
            conn.execute(text("INSERT INTO score_term VALUES (:d, 'starvation', 1, 1, -0.2)"),
                         {"d": i + 1})
            conn.execute(text("INSERT INTO control_action (decision_id, clipped) VALUES"
                              " (:d, :c)"), {"d": i + 1, "c": clipped})
            conn.execute(text("INSERT INTO control_delivery (decision_id, outcome) VALUES"
                              " (:d, :o)"), {"d": i + 1, "o": outcome})
        conn.execute(text("INSERT INTO model_decision (deployment_id, step, ts, score) VALUES"
                          " (2, 0, :ts, 9.0)"), {"ts": t0})
    return url


def test_rankpi_dataset_on_postgres(rankpi_url, settings) -> None:
    import pandas as pd

    from oran_adapt.core.errors import DatasetNotFoundError, DataSourceNotAllowedError
    from oran_adapt.datastore.access import DataAccess
    from oran_adapt.plugins import resolve

    port = resolve("dataset", "rankpi", config_key="dataset_backends").factory(
        settings.model_copy(update={"dataset_rankpi_url": _secret(rankpi_url),
                                    "dataset_rankpi_relations": ["v_*", "e2sm_mac_ue"]}))
    uri = "rankpi:///v_slice_kpi?run_id=5&columns=ts,sst,sd,dl_thp_kbps"
    with port.open(uri) as fh:
        frame = pd.read_csv(fh)
    assert len(frame) == 20 and frame["ts"].is_monotonic_increasing
    assert frame["ts"].iloc[0].endswith("+00:00")  # timestamptz kept its offset
    scan = DataAccess([port]).scan(uri, fmt="csv", timestamp_column="ts")
    assert scan.row_count == 20 and scan.fingerprint == port.stat(uri).fingerprint
    with port.open("rankpi:///v_slice_kpi?run_id=5&sd=2&from=2026-09-29T12:00:05+00:00") as fh:
        assert len(pd.read_csv(fh)) == 5
    for bad, error in (("rankpi:///v_slice_kpi?run_id=6", DataSourceNotAllowedError),
                       ("rankpi:///v_slice_kpi?run_id=7", DataSourceNotAllowedError),
                       ("rankpi:///v_slice_kpi?run_id=99", DatasetNotFoundError),
                       ("rankpi:///e2sm_mac_ue?run_id=5", DataSourceNotAllowedError),
                       ("rankpi:///v_missing?run_id=5", DatasetNotFoundError)):
        with pytest.raises(error):
            port.stat(bad)


def test_rankpi_reads_run_in_a_read_only_transaction(rankpi_url, settings) -> None:
    from sqlalchemy import create_engine, text

    from oran_adapt.adapters.datasets_sql import SqlDataset

    port = SqlDataset("rankpi", {"": rankpi_url}, ["v_*"], time_column="ts", max_bytes=1 << 20,
                      statement_timeout_s=7)
    with port._engine("").connect() as raw:
        conn = port._begin(raw)
        assert conn.execute(text("SHOW transaction_read_only")).scalar() == "on"
        assert conn.execute(text("SHOW statement_timeout")).scalar() == "7s"
        with pytest.raises(Exception, match="read-only"):
            conn.execute(text("DELETE FROM run"))
    with create_engine(rankpi_url).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM run")).scalar() == 3


def test_rankpi_rollout_metrics_on_postgres(rankpi_url) -> None:
    from datetime import timedelta

    from oran_adapt.adapters.rollout_metrics_rankpi import RankpiRolloutMetrics
    from oran_adapt.ports import ArmWindow

    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    port = RankpiRolloutMetrics(rankpi_url, model_names={"slice-model": "ppo_slice"})
    port.ping()
    stats = port.observe(None, ArmWindow(rollout_id="r", model="slice-model", arm="candidate",
                                         version="3", start=t0, end=t0 + timedelta(minutes=1)))
    assert stats.count == 3
    assert stats.samples["score"] == [0.4, 0.6, 0.5]
    assert stats.samples["score_term.starvation"] == [-0.2, -0.2, -0.2]
    assert sorted(stats.samples["clipped"]) == [0.0, 0.0, 1.0]
    assert sorted(stats.samples["delivery_failed"]) == [0.0, 0.0, 1.0]


def _secret(value: str):
    from pydantic import SecretStr

    return SecretStr(value)
