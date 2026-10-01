"""Hardening Phase 5 (finding #4): data versions registered by reference, read in bounded
batches through dataset adapters, hashed like inline data, refused when the object changed or
is too large; CDC from any table shape; the dataset conformance suite on all five adapters."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pandas as pd
import pytest
from sqlalchemy import text

from oran_adapt.adapters.datasets import FileDataset, FsspecDataset, HttpDataset
from oran_adapt.adapters.datasets_cloud import GcsDataset, S3Dataset
from oran_adapt.bootstrap import build_data_access
from oran_adapt.cdc import materialize_cdc, run_cdc_once
from oran_adapt.cdc.events import CdcRowMapping, from_debezium
from oran_adapt.cdc.triggers import trigger_sql
from oran_adapt.cli import main as cli_main
from oran_adapt.conformance import dataset as conformance
from oran_adapt.core.errors import (
    ConfigurationError,
    DataSourceChangedError,
    DataSourceNotAllowedError,
    DataTooLargeError,
    DataVersionConflictError,
)
from oran_adapt.datastore import ingest_version, snapshot_training_data
from oran_adapt.datastore.access import DataAccess
from oran_adapt.datastore.versioning import STORAGE_DERIVED, storage_of, version_row
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import DataRecord, ModelMetadata

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _frame(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": [(T0 + timedelta(minutes=i)).isoformat() for i in range(n)],
        "cell": [f"c{i % 3}" for i in range(n)],
        "prb_util": [round(0.05 * i, 2) for i in range(n)],
        "users": [10 + i for i in range(n)],
    })


@pytest.fixture
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def access(data_dir) -> DataAccess:
    return DataAccess([FileDataset([str(data_dir)])], chunk_rows=3)


def _csv(data_dir: Path, name: str, frame: pd.DataFrame) -> str:
    path = data_dir / name
    frame.to_csv(path, index=False)
    return path.as_uri()


# ---- by reference: same hash, same rows, nothing copied -------------------------------------
@pytest.mark.parametrize("fmt", ["csv", "parquet", "jsonl"])
def test_reference_hashes_and_reads_like_inline(session_factory, access, data_dir, fmt) -> None:
    frame = _frame()
    path = data_dir / f"cells.{fmt}"
    if fmt == "csv":
        frame.to_csv(path, index=False)
    elif fmt == "parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_json(path, orient="records", lines=True)
    with session_scope(session_factory) as s:
        inline = ingest_version(s, "kpi", "inline", frame, kind="HISTORICAL",
                                timestamp_column="ts")
        ref = access.register(s, "kpi", "ref", path.as_uri(), kind="HISTORICAL",
                              timestamp_column="ts")
        assert ref.created and ref.row_count == 10
        assert ref.content_hash == inline.content_hash
        assert (ref.data_start, ref.data_end) == (inline.data_start, inline.data_end)
        dv_ref, dv_inline = version_row(s, "kpi", "ref"), version_row(s, "kpi", "inline")
        assert storage_of(dv_ref) == "reference" and dv_ref.storage_uri == path.as_uri()
        assert s.query(DataRecord).filter_by(data_version_id=dv_ref.id).count() == 0
        a = [(r.observed_at, r.payload) for r in access.iter_rows(s, dv_ref)]
        b = [(r.observed_at, r.payload) for r in access.iter_rows(s, dv_inline)]
        assert a == b
        assert access.verify(s, dv_ref)["matches"] and access.verify(s, dv_inline)["matches"]


def test_register_is_idempotent_and_write_once(session_factory, access, data_dir) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    other = _csv(data_dir, "b.csv", _frame(4))
    with session_scope(session_factory) as s:
        first = access.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
        again = access.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
        assert first.created and not again.created
        with pytest.raises(DataVersionConflictError):
            access.register(s, "kpi", "v1", other, kind="HISTORICAL", timestamp_column="ts")


def test_changed_object_is_refused_on_read_and_fails_verify(
    session_factory, access, data_dir
) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    with session_scope(session_factory) as s:
        access.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
    _csv(data_dir, "a.csv", _frame(12))  # replaced in place
    with session_scope(session_factory) as s:
        dv = version_row(s, "kpi", "v1")
        with pytest.raises(DataSourceChangedError):
            access.load_records(s, [dv.id])
        result = access.verify(s, dv)
        assert not result["matches"] and not result["fingerprint_matches"]


def test_hash_mode_catches_a_change_the_fingerprint_misses(
    session_factory, data_dir
) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    hashing = DataAccess([FileDataset([str(data_dir)])], verify_on_read="hash")
    with session_scope(session_factory) as s:
        hashing.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
        dv = version_row(s, "kpi", "v1")
        # Simulate a store whose fingerprint did not move: forget it, so only the hash can tell.
        dv.extra = {**dv.extra, "reference": {**dv.extra["reference"], "fingerprint": None}}
    changed = _frame()
    changed.loc[3, "users"] = 99
    _csv(data_dir, "a.csv", changed)
    with session_scope(session_factory) as s, pytest.raises(DataSourceChangedError):
        hashing.load_records(s, [version_row(s, "kpi", "v1").id])


def test_limits_are_enforced_before_reading(session_factory, data_dir) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    file = FileDataset([str(data_dir)])
    with pytest.raises(DataTooLargeError) as small_source:
        DataAccess([file], max_source_bytes=10).scan(uri, timestamp_column="ts")
    assert small_source.value.context["key"] == "DATASET_MAX_SOURCE_BYTES"
    with session_scope(session_factory) as s:
        DataAccess([file]).register(s, "kpi", "v1", uri, kind="HISTORICAL",
                                    timestamp_column="ts")
        dv = version_row(s, "kpi", "v1")
        with pytest.raises(DataTooLargeError) as ceiling:
            DataAccess([file], max_rows=5).load_records(s, [dv.id])
        assert ceiling.value.context["key"] == "DATASET_MAX_ROWS"


def test_analysis_sample_is_bounded_and_spread(session_factory, data_dir) -> None:
    uri = _csv(data_dir, "a.csv", _frame(20))
    sampler = DataAccess([FileDataset([str(data_dir)])], chunk_rows=4, analysis_max_rows=5)
    with session_scope(session_factory) as s:
        sampler.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
        rows = sampler.sample(s, version_row(s, "kpi", "v1"))
    assert [r.payload["users"] for r in rows] == [10, 14, 18, 22, 26]


def test_uris_outside_the_allow_list_are_refused(data_dir, tmp_path) -> None:
    outside = tmp_path / "elsewhere.csv"
    _frame().to_csv(outside, index=False)
    access = DataAccess([FileDataset([str(data_dir)])])
    for uri in (outside.as_uri(), (data_dir / ".." / "elsewhere.csv").as_uri(),
                "https://example.org/a.csv"):
        with pytest.raises(DataSourceNotAllowedError):
            access.scan(uri, timestamp_column="ts")
    with pytest.raises(DataSourceNotAllowedError):
        DataAccess().backend_for("file:///x.csv")  # no adapter enabled at all


def test_training_snapshot_of_referenced_data_is_derived(session_factory, access,
                                                        data_dir) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    with session_scope(session_factory) as s:
        s.add(ModelMetadata(model_id="m", mlflow_model_name="m", model_type="regressor",
                            framework="sklearn", task_type="regressor", target_column="users"))
        base = access.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
        rows = access.load_records(s, [base.data_version_id])
        held_out = {rows[0].id, rows[5].id}
        snap = snapshot_training_data(
            s, model_id="m", model_version="2", source_version_ids=[base.data_version_id],
            parent_version_id=base.data_version_id, exclude_record_ids=held_out, access=access,
        )
        dv = version_row(s, "kpi", snap.version)
        assert storage_of(dv) == STORAGE_DERIVED and snap.row_count == 8
        kept = access.load_records(s, [dv.id])
        assert [r.payload for r in kept] == [r.payload for r in rows if r.id not in held_out]
        expected = ingest_version(
            s, "kpi", "copy",
            pd.DataFrame([{**r.payload, "ts": r.observed_at} for r in kept]),
            kind="HISTORICAL", timestamp_column="ts",
        )
        assert snap.content_hash == expected.content_hash
        assert access.verify(s, dv)["matches"]


def test_build_data_access_resolves_configured_adapters(settings, data_dir) -> None:
    configured = settings.model_copy(update={"dataset_backends": "file,fsspec",
                                             "dataset_file_roots": [str(data_dir)],
                                             "dataset_fsspec_prefixes": ["memory://p5/"]})
    access = build_data_access(configured)
    assert [type(b).__name__ for b in access.backends] == ["FileDataset", "FsspecDataset"]
    assert build_data_access(settings).backends == []
    with pytest.raises(ConfigurationError):
        build_data_access(settings.model_copy(update={"dataset_backends": "file"}))


# ---- conformance: every adapter ------------------------------------------------------------
class _Store(BaseHTTPRequestHandler):
    """A tiny object server: plain URLs for the http adapter, the Cloud Storage JSON API paths
    (every write a new generation) for the gcs adapter."""

    objects: ClassVar[dict[str, list[bytes]]] = {}

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if "/storage/v1/b/" in parts.path:
            _, _, rest = parts.path.partition("/storage/v1/b/")
            bucket, _, key = rest.partition("/o/")
            history = self.objects.get(f"{bucket}/{unquote(key)}")
            if not history:
                return self._send(404)
            gen = int(query.get("generation", [len(history)])[0])
            data = history[gen - 1]
            if parts.path.startswith("/download/"):
                return self._send(200, data)
            meta = {"generation": str(gen), "md5Hash": str(hash(data)), "size": str(len(data))}
            return self._send(200, json.dumps(meta).encode(),
                              {"Content-Type": "application/json"})
        history = self.objects.get(parts.path.lstrip("/"))
        if not history:
            return self._send(404)
        data = history[-1]
        return self._send(200, data, {"ETag": f'"{len(history)}-{len(data)}"'})


@pytest.fixture(scope="module")
def store() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _S3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _S3:
    """head_object / get_object over versioned in-memory buckets."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], list[bytes]] = {}

    def _get(self, Bucket: str, Key: str, VersionId: str | None = None) -> tuple[bytes, str]:
        history = self.objects.get((Bucket, Key))
        if not history:
            raise _S3Error("NoSuchKey")
        index = int(VersionId) if VersionId else len(history)
        return history[index - 1], str(index)

    def head_object(self, **kw) -> dict:
        data, version = self._get(**kw)
        return {"ETag": f'"{version}-{len(data)}"', "VersionId": version,
                "ContentLength": len(data)}

    def get_object(self, **kw) -> dict:
        return {"Body": io.BytesIO(self._get(**kw)[0])}


def _adapter(name: str, tmp_path: Path, store: str) -> tuple[object, conformance.Context, bool]:
    if name == "file":
        root = tmp_path / "root"
        root.mkdir()

        def put_file(key: str, data: bytes) -> str:
            (root / key).write_bytes(data)
            return (root / key).as_uri()

        return (FileDataset([str(root)]),
                conformance.Context(put=put_file, missing=lambda k: (root / k).as_uri(),
                                    outside=(tmp_path / "x.csv").as_uri()), False)
    if name == "fsspec":
        import fsspec

        fs = fsspec.filesystem("memory")
        prefix = f"memory://p5-{tmp_path.name}"

        def put_mem(key: str, data: bytes) -> str:
            fs.pipe_file(f"{prefix}/{key}", data)
            return f"{prefix}/{key}"

        return (FsspecDataset([prefix], lambda protocol: fs),
                conformance.Context(put=put_mem, missing=lambda k: f"{prefix}/{k}",
                                    outside="memory://elsewhere/x.csv"), False)
    if name == "http":
        def put_http(key: str, data: bytes) -> str:
            _Store.objects.setdefault(key, []).append(data)
            return f"http://{store}/{key}"

        return (HttpDataset(["127.0.0.1"], allow_plain=True, client=httpx.Client(timeout=5),
                            max_bytes=1 << 20),
                conformance.Context(put=put_http, missing=lambda k: f"http://{store}/{k}",
                                    outside="http://example.org/x.csv"), False)
    if name == "gcs":
        def put_gcs(key: str, data: bytes) -> str:
            _Store.objects.setdefault(f"kpi/{key}", []).append(data)
            return f"gs://kpi/{key}"

        return (GcsDataset(["kpi"], endpoint=f"http://{store}", client=httpx.Client(timeout=5),
                           token=lambda: None, max_bytes=1 << 20),
                conformance.Context(put=put_gcs, missing=lambda k: f"gs://kpi/{k}",
                                    outside="gs://other/x.csv"), True)
    s3 = _S3()

    def put_s3(key: str, data: bytes) -> str:
        s3.objects.setdefault(("kpi", key), []).append(data)
        return f"s3://kpi/{key}"

    return (S3Dataset(["kpi"], client=s3, errors=(_S3Error,), code_of=lambda e: e.code,
                      max_bytes=1 << 20),
            conformance.Context(put=put_s3, missing=lambda k: f"s3://kpi/{k}",
                                outside="s3://other/x.csv"), True)


@pytest.mark.parametrize("name", ["file", "fsspec", "http", "gcs", "s3"])
def test_dataset_adapter_conformance(name, tmp_path, store) -> None:
    port, ctx, pinned = _adapter(name, tmp_path, store)
    ran = conformance.run(port, ctx, pinned=pinned)  # type: ignore[arg-type]
    assert "rows_stable" in ran and ("pinned_stable" in ran) == pinned


def test_pinned_reference_survives_an_overwrite(session_factory, tmp_path, store) -> None:
    port, ctx, _ = _adapter("s3", tmp_path, store)
    access = DataAccess([port])  # type: ignore[list-item]
    uri = ctx.put("cells.csv", _frame().to_csv(index=False).encode())
    with session_scope(session_factory) as s:
        info = access.register(s, "kpi", "v1", uri, kind="HISTORICAL", timestamp_column="ts")
    ctx.put("cells.csv", _frame(3).to_csv(index=False).encode())
    with session_scope(session_factory) as s:
        rows = access.load_records(s, [info.data_version_id])
        assert len(rows) == 10  # the registered object version, not the overwrite
        assert version_row(s, "kpi", "v1").extra["reference"]["pinned_uri"].endswith(
            "?versionId=1")


# ---- CDC from any table --------------------------------------------------------------------
def test_trigger_sql_refuses_unsafe_identifiers() -> None:
    with pytest.raises(ConfigurationError):
        trigger_sql("cells; DROP TABLE x", dialect="sqlite", columns=["id"])
    with pytest.raises(ConfigurationError):
        trigger_sql("cells", dialect="sqlite", columns=["cell"])  # key column missing
    pg = trigger_sql("cell_kpi", dialect="postgresql", key_column="sample_id")
    assert "row_to_json(NEW)" in pg[0] and "NEW.sample_id" in pg[0]


def test_polling_cdc_follows_a_table_with_its_own_columns(migrated_settings,
                                                         session_factory) -> None:
    if not migrated_settings.database_url.startswith("sqlite"):
        pytest.skip("creates a SQLite source table [owner=TNDEVIL4U expires=2027-03-31]")
    with session_scope(session_factory) as s:
        s.execute(text("CREATE TABLE cell_kpi (sample_id INTEGER PRIMARY KEY, cell TEXT, "
                       "ts TEXT, prb_util REAL)"))
        for statement in trigger_sql("cell_kpi", dialect="sqlite", key_column="sample_id",
                                     columns=["sample_id", "cell", "ts", "prb_util"]):
            s.execute(text(statement))
    with session_scope(session_factory) as s:
        for i in range(3):
            s.execute(text("INSERT INTO cell_kpi VALUES (:i, :c, :t, :p)"),
                      {"i": i + 1, "c": f"c{i}", "t": (T0 + timedelta(minutes=i)).isoformat(),
                       "p": 0.1 * i})
    with session_scope(session_factory) as s:
        s.execute(text("UPDATE cell_kpi SET prb_util = 0.9 WHERE sample_id = 2"))
        s.execute(text("DELETE FROM cell_kpi WHERE sample_id = 3"))

    mapped = migrated_settings.model_copy(update={
        "cdc_mode": "polling", "cdc_polling_table": "cell_kpi", "cdc_key_column": "sample_id",
        "cdc_dataset_column": "", "cdc_dataset_id": "cells", "cdc_time_column": "ts",
        "cdc_payload_column": "",
    })
    summary = run_cdc_once(session_factory, mapped)
    assert summary["stored"] == 5
    with session_scope(session_factory) as s:
        version = materialize_cdc(s, "cells", max_tx_ids=1000)
        assert version is not None and version.row_count == 2
        rows = DataAccess().load_records(s, [version.data_version_id])
    assert [r.payload for r in rows] == [{"cell": "c0", "prb_util": 0.0},
                                         {"cell": "c1", "prb_util": 0.9}]


def test_debezium_messages_map_through_the_same_columns() -> None:
    mapping = CdcRowMapping(key_column="sample_id", dataset_column="", dataset_id="cells",
                            time_column="ts", payload_column="")
    message = {"payload": {
        "op": "c", "before": None, "ts_ms": 1,
        "after": {"sample_id": 7, "cell": "c7", "ts": 1_788_220_800_000_000, "prb_util": 0.5},
        "source": {"table": "cell_kpi", "txId": 42, "lsn": 1},
    }}
    event = from_debezium(message, topic="t", partition=0, offset=3, schema_ref="s",
                          mapping=mapping)
    assert event is not None
    assert event.new_value == {"id": 7, "dataset_id": "cells",
                               "observed_at": "2026-09-01T00:00:00+00:00",
                               "payload": {"cell": "c7", "prb_util": 0.5}}
    assert event.primary_key == "7" and event.dataset_id == "cells"


# ---- API and CLI ---------------------------------------------------------------------------
@pytest.fixture
def ref_client(migrated_settings, data_dir):
    from fastapi.testclient import TestClient

    from oran_adapt.api.app import create_app

    configured = migrated_settings.model_copy(update={"dataset_backends": "file",
                                                      "dataset_file_roots": [str(data_dir)]})
    with TestClient(create_app(configured)) as c:
        yield c


def test_api_registers_pages_and_verifies_by_reference(ref_client, data_dir) -> None:
    uri = _csv(data_dir, "a.csv", _frame())
    base = "/api/v1/datasets/kpi/versions"
    body = {"version": "v1", "storage_uri": uri, "timestamp_column": "ts"}
    assert ref_client.post(base, json=body).status_code == 201
    assert ref_client.post(base, json=body).status_code == 200
    page = ref_client.get(f"{base}/v1/rows", params={"offset": 8, "limit": 5}).json()
    assert page["storage"] == "reference" and page["total_rows"] == 10
    assert [r["payload"]["users"] for r in page["rows"]] == [18, 19]
    assert ref_client.post(f"{base}/v1/verify").json()["matches"] is True

    both = {**body, "records": [{"a": 1}]}
    assert ref_client.post(base, json=both).status_code == 422
    denied = ref_client.post(base, json={**body, "version": "v2",
                                         "storage_uri": "file:///etc/passwd"})
    assert denied.status_code == 422 and denied.json()["code"] == "DATA_SOURCE_NOT_ALLOWED"
    _csv(data_dir, "a.csv", _frame(11))
    assert ref_client.get(f"{base}/v1/rows").status_code == 409
    assert ref_client.post(f"{base}/v1/verify").json()["matches"] is False


def test_cli_register_verify_and_trigger_sql(migrated_settings, data_dir, monkeypatch,
                                            capsys) -> None:
    configured = migrated_settings.model_copy(update={"dataset_backends": "file",
                                                      "dataset_file_roots": [str(data_dir)]})
    monkeypatch.setattr("oran_adapt.cli.get_settings", lambda: configured)
    uri = _csv(data_dir, "a.csv", _frame())
    assert cli_main(["data", "register", "--dataset", "kpi", "--version", "v1", "--uri", uri,
                     "--timestamp-column", "ts"]) == 0
    assert json.loads(capsys.readouterr().out)["row_count"] == 10
    assert cli_main(["data", "verify", "--dataset", "kpi", "--version", "v1"]) == 0
    assert json.loads(capsys.readouterr().out)["matches"] is True
    _csv(data_dir, "a.csv", _frame(9))
    assert cli_main(["data", "verify", "--dataset", "kpi", "--version", "v1"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "DATA_SOURCE_CHANGED"
    assert cli_main(["cdc", "trigger-sql", "--table", "cell_kpi", "--dialect", "sqlite",
                     "--columns", "id,cell,ts"]) == 0
    assert "CREATE TRIGGER cell_kpi_cdc_insert" in capsys.readouterr().out
