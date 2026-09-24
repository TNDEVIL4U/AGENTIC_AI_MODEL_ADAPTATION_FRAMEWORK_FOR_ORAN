"""Phase 14 Stage C: change data capture (trigger changelog + polling fallback, Debezium/Kafka
consumer), idempotent CDC events, CDC data versions, persisted CurrentData."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import OperationalError
from test_phase14_member1 import (  # shared seeding helpers (same test directory)
    _fit,
    _load_regime,
    _rsrp_regime,
    _seed,
    _submit,
)

from oran_adapt import cli
from oran_adapt.cdc import (
    KafkaCdcSource,
    PollingCdcSource,
    build_source,
    from_debezium,
    materialize_cdc,
    run_cdc_once,
)
from oran_adapt.core.enums import CdcOperation, DataKind, JobStatus
from oran_adapt.core.errors import (
    CdcProcessingError,
    CdcUnavailableError,
)
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.datastore import ingest_version
from oran_adapt.datastore.current_data import clean_records, get_current_data
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    AuditLog,
    CdcEventRecord,
    CdcOffset,
    DataRecord,
    DataVersion,
    KpiSample,
    ModelVersionEvaluation,
)
from oran_adapt.registry.client import MlflowRegistry

T0 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


@pytest.fixture
def polling(migrated_settings):
    return migrated_settings.model_copy(update={"cdc_mode": "polling"})


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _kpi_changes(session_factory, dataset: str = "kpi") -> None:
    """3 inserts, then row 2 updated and row 3 deleted: 5 row changes."""
    with session_scope(session_factory) as s:
        for i in range(3):
            s.add(KpiSample(
                id=i + 1, dataset_id=dataset, observed_at=T0 + timedelta(minutes=i),
                payload={"prb_util": 0.1 * i, "rsrp": -90.0 - i, "label": i % 2},
            ))
    with session_scope(session_factory) as s:
        s.get(KpiSample, 2).payload = {"prb_util": 0.99, "rsrp": -80.0, "label": 1}
    with session_scope(session_factory) as s:
        s.delete(s.get(KpiSample, 3))


# ---- trigger changelog + polling consumer ---------------------------------------------------
def test_insert_update_delete_become_complete_cdc_events(session_factory, polling) -> None:
    inserts_before = _sample("cdc_events_total", {"source": "polling", "operation": "INSERT"})
    _kpi_changes(session_factory)

    summary = run_cdc_once(session_factory, polling)
    assert summary["fetched"] == summary["stored"] == 5
    assert summary["operations"] == {"INSERT": 3, "UPDATE": 1, "DELETE": 1}

    with session_scope(session_factory) as s:
        events = s.query(CdcEventRecord).order_by(CdcEventRecord.id).all()
        assert [e.operation for e in events] == ["INSERT"] * 3 + ["UPDATE", "DELETE"]
        update, delete = events[3], events[4]
        # Every spec field: id, table, operation, key, old/new images, time, offset, schema.
        assert len(update.event_id) == 64 and update.source_table == "kpi_sample"
        assert update.primary_key == "2" and update.dataset_id == "kpi"
        assert update.old_value["payload"]["prb_util"] == pytest.approx(0.1)
        assert update.new_value["payload"]["prb_util"] == pytest.approx(0.99)
        assert delete.new_value is None and delete.old_value["id"] == 3
        assert update.event_ts is not None and update.source_offset == "4"
        assert update.schema_version == "kpi_sample/1"
        assert s.get(CdcOffset, "polling:kpi_sample").position == "5"

    assert _sample(
        "cdc_events_total", {"source": "polling", "operation": "INSERT"}
    ) == inserts_before + 3
    assert _sample("cdc_processing_lag") >= 0
    assert run_cdc_once(session_factory, polling)["fetched"] == 0  # offset moved past them


def test_duplicate_cdc_events_are_idempotent(session_factory, polling) -> None:
    """Scenario 8: the offset is lost (consumer crashed after storing, before its offset
    survived) and the whole changelog is read again - nothing is stored twice."""
    _kpi_changes(session_factory)
    run_cdc_once(session_factory, polling)
    before = _sample("cdc_events_total", {"source": "polling", "operation": "INSERT"})

    with session_scope(session_factory) as s:
        s.get(CdcOffset, "polling:kpi_sample").position = "0"
    again = run_cdc_once(session_factory, polling)

    assert (again["fetched"], again["stored"], again["duplicates"]) == (5, 0, 5)
    with session_scope(session_factory) as s:
        assert s.scalar(select(func.count()).select_from(CdcEventRecord)) == 5
    assert _sample("cdc_events_total", {"source": "polling", "operation": "INSERT"}) == before


# ---- CDC data versions ------------------------------------------------------------------------
def test_cdc_events_materialize_into_immutable_versions(session_factory, polling) -> None:
    _kpi_changes(session_factory)
    run_cdc_once(session_factory, polling)

    with session_scope(session_factory) as s:
        first = materialize_cdc(s, "kpi")
    assert first is not None and first.kind == DataKind.CDC
    assert first.row_count == 2  # rows 1 and 2 remain; row 3 was deleted
    assert first.cdc_range["event_count"] == 5
    assert (first.cdc_range["first_offset"], first.cdc_range["last_offset"]) == ("1", "5")
    assert first.status == "AVAILABLE" and len(first.schema_hash) == 64
    assert first.storage_uri == f"db://data_record?data_version_id={first.data_version_id}"
    assert first.source.startswith("cdc:polling")
    with session_scope(session_factory) as s:
        rows = s.query(DataRecord).filter_by(data_version_id=first.data_version_id).all()
        assert {r.record_key: r.payload["prb_util"] for r in rows} == pytest.approx(
            {"1": 0.0, "2": 0.99}
        )
        assert s.get(DataVersion, first.data_version_id).extra["deleted_keys"] == ["3"]
        assert all(e.data_version_id == first.data_version_id
                   for e in s.query(CdcEventRecord).all())
        assert materialize_cdc(s, "kpi") is None  # nothing pending: harmless to repeat

    # Row 1 is deleted later: the next version chains to the first and records the deletion.
    with session_scope(session_factory) as s:
        s.delete(s.get(KpiSample, 1))
        s.add(KpiSample(id=4, dataset_id="kpi", observed_at=T0 + timedelta(minutes=9),
                        payload={"prb_util": 0.4, "rsrp": -95.0, "label": 0}))
    run_cdc_once(session_factory, polling)
    with session_scope(session_factory) as s:
        second = materialize_cdc(s, "kpi")
    assert second.parent_version == first.version and second.row_count == 1

    # CurrentData cleaning over both versions: row 1 is gone, row 2 kept once.
    with session_scope(session_factory) as s:
        records = s.query(DataRecord).filter(
            DataRecord.data_version_id.in_([first.data_version_id, second.data_version_id])
        ).all()
        cleaned = clean_records(s, records, required_columns=["label"])
        assert sorted(r.record_key for r in cleaned.records) == ["2", "4"]
        assert cleaned.quality["deleted_removed"] == 1


def test_clean_records_dedupes_resolves_conflicts_and_validates_schema(session_factory) -> None:
    base = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0], "label": [0, 1, None, 1]})
    with session_scope(session_factory) as s:
        v1 = ingest_version(s, "clean", "v1", base, kind="HISTORICAL", start=T0)
        # v2 repeats v1's first row exactly (same time and values): a duplicate.
        v2 = ingest_version(s, "clean", "v2", base.iloc[:1], kind="DRIFTED", start=T0)
        keyed = pd.DataFrame({"x": [7.0, 8.0], "label": [1, 0]})
        k1 = ingest_version(s, "clean", "k1", keyed, kind="CDC", start=T0, record_keys=["a", "b"])
        k2 = ingest_version(s, "clean", "k2", keyed.iloc[:1] * 2, kind="CDC",
                            start=T0 + timedelta(hours=1), record_keys=["a"])
        ids = [v1.data_version_id, v2.data_version_id, k1.data_version_id, k2.data_version_id]
        records = s.query(DataRecord).filter(DataRecord.data_version_id.in_(ids)).all()
        cleaned = clean_records(s, records, required_columns=["label"])

        assert cleaned.quality == {
            "input_rows": 8, "conflicts_resolved": 1, "deleted_removed": 0,
            "duplicates_removed": 1, "schema_rejected": 1, "output_rows": 5,
        }
        # Key "a" resolved to the newer version's row; output ordered by time.
        a_row = next(r for r in cleaned.records if r.record_key == "a")
        assert a_row.payload["x"] == 14.0
        stamps = [r.observed_at for r in cleaned.records]
        assert stamps == sorted(stamps)


# ---- Debezium / Kafka ------------------------------------------------------------------------
def _debezium(op, before, after, *, lsn=1000, tx=77, wrap=True):
    payload = {
        "before": before, "after": after, "op": op, "ts_ms": 1_767_225_600_500,
        "source": {"version": "2.7.0.Final", "connector": "postgresql", "table": "kpi_sample",
                   "txId": tx, "lsn": lsn, "ts_ms": 1_767_225_600_000},
    }
    return json.dumps({"schema": {}, "payload": payload} if wrap else payload).encode()


def _row(i, util, *, as_string=True):
    payload = {"prb_util": util, "rsrp": -90.0, "label": 1}
    return {"id": i, "dataset_id": "kpi", "observed_at": "2026-03-01T00:00:00Z",
            "payload": json.dumps(payload) if as_string else payload}


def test_debezium_envelopes_parse_into_cdc_events() -> None:
    coords = {"topic": "oran.public.kpi_sample", "partition": 0}
    ins = from_debezium(_debezium("c", None, _row(1, 0.5)), offset=10, **coords)
    assert ins.operation == CdcOperation.INSERT and ins.primary_key == "1"
    assert ins.new_value["payload"] == {"prb_util": 0.5, "rsrp": -90.0, "label": 1}
    assert (ins.transaction_id, ins.source_offset) == ("77", "oran.public.kpi_sample:0:10")
    assert ins.timestamp == datetime(2026, 1, 1, tzinfo=UTC)
    assert ins.schema_version == "kpi_sample/1;debezium/2.7.0.Final"

    upd = from_debezium(
        _debezium("u", _row(1, 0.5), _row(1, 0.6, as_string=False), lsn=1001, wrap=False),
        offset=11, **coords,
    )
    assert upd.operation == CdcOperation.UPDATE
    assert (upd.old_value["payload"]["prb_util"], upd.new_value["payload"]["prb_util"]) == (0.5, 0.6)
    dele = from_debezium(_debezium("d", _row(1, 0.6), None, lsn=1002), offset=12, **coords)
    assert dele.operation == CdcOperation.DELETE and dele.new_value is None
    snap = from_debezium(_debezium("r", None, _row(2, 0.1), lsn=900), offset=0, **coords)
    assert snap.operation == CdcOperation.INSERT

    # The same WAL change re-sent at a new Kafka offset keeps its id (dedupe across restarts).
    again = from_debezium(_debezium("c", None, _row(1, 0.5)), offset=99, **coords)
    assert again.event_id == ins.event_id and upd.event_id != ins.event_id

    assert from_debezium(None, offset=13, **coords) is None  # tombstone
    assert from_debezium(json.dumps({"ts_ms": 1}).encode(), offset=14, **coords) is None
    with pytest.raises(CdcProcessingError):
        from_debezium(b"not json", offset=15, **coords)
    with pytest.raises(CdcProcessingError):
        from_debezium(json.dumps({"payload": {"foo": 1}}).encode(), offset=16, **coords)


class _Msg:
    def __init__(self, value, offset, error=None):
        self._value, self._offset, self._error = value, offset, error

    def value(self):
        return self._value

    def topic(self):
        return "oran.public.kpi_sample"

    def partition(self):
        return 0

    def offset(self):
        return self._offset

    def error(self):
        return self._error


class _FakeConsumer:
    """confluent_kafka.Consumer's surface: subscribe/consume/commit/close."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.commits = 0
        self.subscribed: list[str] = []

    def subscribe(self, topics):
        self.subscribed = topics

    def consume(self, num_messages, timeout):
        item = self.batches.pop(0) if self.batches else []
        if isinstance(item, Exception):
            raise item
        return item[:num_messages]

    def commit(self, asynchronous=True):
        self.commits += 1

    def close(self):
        pass


def test_kafka_consumer_commits_after_storing_and_skips_redeliveries(
    session_factory, migrated_settings
) -> None:
    settings = migrated_settings.model_copy(update={"cdc_mode": "kafka"})
    batch = [
        _Msg(_debezium("c", None, _row(1, 0.5), lsn=1), 0),
        _Msg(_debezium("u", _row(1, 0.5), _row(1, 0.7), lsn=2), 1),
        _Msg(None, 2),  # tombstone: acknowledged, not stored
    ]
    fake = _FakeConsumer([batch, batch])  # the second is a redelivery of the same messages
    source = build_source(settings, kafka_consumer=fake)
    assert isinstance(source, KafkaCdcSource) and fake.subscribed == [settings.cdc_kafka_topic]
    before = _sample("cdc_events_total", {"source": "debezium", "operation": "UPDATE"})

    first = run_cdc_once(session_factory, settings, source=source)
    assert (first["stored"], fake.commits) == (2, 1)
    second = run_cdc_once(session_factory, settings, source=source)
    assert (second["stored"], second["duplicates"], fake.commits) == (0, 2, 2)

    with session_scope(session_factory) as s:
        assert s.scalar(select(func.count()).select_from(CdcEventRecord)) == 2
        offset = s.get(CdcOffset, source.name)
        assert json.loads(offset.position) == {"0": 2}
    assert _sample("cdc_events_total", {"source": "debezium", "operation": "UPDATE"}) == before + 1


# ---- chaos: Kafka down, consumer failure ------------------------------------------------------
def test_kafka_unavailable_stores_and_commits_nothing(session_factory, migrated_settings) -> None:
    settings = migrated_settings.model_copy(update={"cdc_mode": "kafka"})
    broken = _FakeConsumer([
        [_Msg(_debezium("c", None, _row(1, 0.5)), 0), _Msg(None, 1, error="_ALL_BROKERS_DOWN")],
        RuntimeError("KafkaException: _TRANSPORT"),
    ])
    source = KafkaCdcSource(settings, broken)
    for _ in range(2):
        with pytest.raises(CdcUnavailableError):
            run_cdc_once(session_factory, settings, source=source)
    assert broken.commits == 0
    with session_scope(session_factory) as s:
        assert s.scalar(select(func.count()).select_from(CdcEventRecord)) == 0
        assert s.get(CdcOffset, source.name) is None


def test_kafka_mode_without_client_library_is_reported(migrated_settings, monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "confluent_kafka", None)  # import fails
    settings = migrated_settings.model_copy(update={"cdc_mode": "kafka"})
    with pytest.raises(CdcUnavailableError, match="confluent-kafka"):
        build_source(settings)
    with pytest.raises(CdcUnavailableError, match="disabled"):
        build_source(migrated_settings)  # cdc_mode defaults to disabled
    assert isinstance(
        build_source(migrated_settings.model_copy(update={"cdc_mode": "polling"})),
        PollingCdcSource,
    )


def test_consumer_failure_rolls_back_and_the_batch_is_redelivered(
    session_factory, polling, monkeypatch
) -> None:
    from oran_adapt.cdc import consumer

    _kpi_changes(session_factory)
    real = consumer.store_events

    def failing(session, events, **kw):
        real(session, events, **kw)  # rows are added, then the database fails
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(consumer, "store_events", failing)
    with pytest.raises(CdcProcessingError):
        run_cdc_once(session_factory, polling)
    with session_scope(session_factory) as s:
        assert s.scalar(select(func.count()).select_from(CdcEventRecord)) == 0
        assert s.get(CdcOffset, "polling:kpi_sample") is None

    monkeypatch.setattr(consumer, "store_events", real)
    assert run_cdc_once(session_factory, polling)["stored"] == 5


# ---- CurrentData persisted and traceable ------------------------------------------------------
def test_job_persists_current_data_and_every_score_refers_to_it(
    session_factory, registry, migrated_settings, client, tmp_path
) -> None:
    signal_world = _rsrp_regime(120, seed=10)
    load_world = _load_regime(120, seed=20)
    _seed(
        session_factory, registry, migrated_settings, model_id="cell-cd", name="cell_cd",
        models=[_fit(signal_world), _fit(load_world), _fit(_rsrp_regime(120, seed=12))],
        live="3", historical=signal_world, drifted=_load_regime(100, seed=21),
    )
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-cd", event_id="evt-cd", model_version="3"), tmp_path,
    )
    assert job.status == JobStatus.COMPLETED, job.error
    assert job.result["outcome"] == "REUSED"
    cd_id = job.result["current_data_id"]
    assert cd_id

    # "Which CurrentData version was used?" - from the job, the API, the CLI or the audit log.
    with session_scope(session_factory) as s:
        cd = get_current_data(s, cd_id)
        dv = s.get(DataVersion, cd["data_version_id"])
        assert dv.kind == DataKind.CURRENT and dv.row_count == cd["row_count"] > 0
        assert cd["job_id"] == job.job_id and cd["model_id"] == "cell-cd"
        assert [v["version"] for v in cd["source_versions"]] == ["hist-1", "drift-1"]
        assert cd["quality"]["evaluation_rows"] == cd["row_count"]
        assert cd["quality"]["duplicates_removed"] == 0
        assert cd["content_hash"] == dv.content_hash and len(cd["schema_hash"]) == 64
        assert cd["data_start"] <= cd["data_end"]
        scores = s.query(ModelVersionEvaluation).filter_by(job_id=job.job_id).all()
        assert len(scores) == 3 and {e.current_data_id for e in scores} == {cd_id}
        audit = s.query(AuditLog).filter_by(
            job_id=job.job_id, action="CURRENT_DATA_CREATED"
        ).one()
        assert audit.detail["current_data_id"] == cd_id

    r = client.get(f"/api/v1/current-data/{cd_id}")
    assert r.status_code == 200 and r.json()["data_version"] == dv.version
    assert client.get("/api/v1/current-data", params={"model_id": "cell-cd"}).json()[0][
        "current_data_id"
    ] == cd_id
    assert client.get("/api/v1/current-data/nope").status_code == 404


# ---- CLI, API, migration ---------------------------------------------------------------------
def test_cli_cdc_run_and_materialize(session_factory, migrated_settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: migrated_settings)
    _kpi_changes(session_factory, dataset="cli-kpi")

    assert cli.main(["cdc", "run", "--mode", "polling", "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["stored"] == 5
    assert cli.main(["cdc", "materialize", "--dataset", "cli-kpi"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["materialized"] is True and out["kind"] == "CDC" and out["row_count"] == 2
    assert cli.main(["cdc", "materialize", "--dataset", "cli-kpi"]) == 0
    assert json.loads(capsys.readouterr().out)["materialized"] is False
    assert cli.main(["cdc", "run", "--once"]) == 1  # CDC_MODE=disabled
    assert json.loads(capsys.readouterr().out)["code"] == "CDC_UNAVAILABLE"


def test_api_materializes_cdc_events(session_factory, polling, client) -> None:
    _kpi_changes(session_factory, dataset="api-kpi")
    run_cdc_once(session_factory, polling)
    r = client.post("/api/v1/datasets/api-kpi/cdc/materialize")
    assert r.status_code == 201 and r.json()["cdc_range"]["event_count"] == 5
    assert client.post("/api/v1/datasets/api-kpi/cdc/materialize").json()["materialized"] is False


def test_migration_0005_adds_cdc_and_current_data_schema(migrated_settings) -> None:
    insp = sa_inspect(create_db_engine(migrated_settings.database_url))
    tables = set(insp.get_table_names())
    assert {"kpi_sample", "cdc_changelog", "cdc_event", "cdc_offset", "current_data"} <= tables
    dv_cols = {c["name"] for c in insp.get_columns("data_version")}
    assert {"source", "schema_hash", "storage_uri", "status", "cdc_range", "source_tx"} <= dv_cols
    assert "record_key" in {c["name"] for c in insp.get_columns("data_record")}
    assert "current_data_id" in {c["name"] for c in insp.get_columns("model_version_evaluation")}
    unique = [i for i in insp.get_indexes("cdc_event") if i["column_names"] == ["event_id"]]
    assert unique and unique[0]["unique"]
