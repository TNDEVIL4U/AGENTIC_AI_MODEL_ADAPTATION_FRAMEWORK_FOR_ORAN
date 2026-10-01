"""Hardening Phase 5 acceptance: data access by reference, checked end to end.

Run by scripts/verify.sh 5 after lint, the import-boundary test and the scoped tests.

1. Bounded memory: a 100 000-row object is registered and read back in batches; the peak
   allocation while doing so is a small fraction of loading it whole.
2. The same rows get the same content hash whether sent inline or registered by reference
   (CSV, Parquet, JSON Lines); nothing is copied into the database; the rows read back equal;
   a training snapshot of referenced data is a derived version and verifies.
3. A source that changed after registration, one over DATASET_MAX_SOURCE_BYTES, a read over
   DATASET_MAX_ROWS and a URI outside the allow-list are all refused; the API answers
   409 / 422 and the CLI verify fails.
4. Every installed dataset adapter passes the dataset conformance suite (file and memory
   stores for real; HTTP and GCS against a local server; S3 against a client double -
   unverified against real S3 / GCS).
5. CDC follows a source table with its own column names (generated triggers, column mapping),
   and Debezium messages map through the same columns.
6. Vendor SDKs stay inside their adapters and every dataset adapter is documented in
   docs/adapters/dataset.md.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import tracemalloc
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 5 tests and their doubles


def _settings(tmp: Path):
    from oran_adapt.core.config import Settings
    from oran_adapt.db.migrate import upgrade_to_head

    (tmp / "mlartifacts").mkdir(exist_ok=True)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp / "work"),
        log_json=False,
        auth_enabled=False,
        notification_dispatch_enabled=False,
    )
    upgrade_to_head(settings.database_url)
    return settings


def _factory(settings):
    from oran_adapt.db.base import create_db_engine, make_session_factory

    return make_session_factory(create_db_engine(settings.database_url))


def bounded_memory(tmp: Path) -> str:
    import pandas as pd
    import test_phase5_data_by_reference as t

    from oran_adapt.adapters.datasets import FileDataset
    from oran_adapt.datastore.access import DataAccess
    from oran_adapt.datastore.versioning import version_row
    from oran_adapt.db.base import session_scope

    rows = 100_000
    data = tmp / "data"
    data.mkdir()
    t._frame(rows).to_csv(data / "big.csv", index=False)
    size_mb = (data / "big.csv").stat().st_size / 2**20
    factory = _factory(_settings(tmp))
    access = DataAccess([FileDataset([str(data)])], chunk_rows=5_000,
                        analysis_max_rows=1_000)

    tracemalloc.start()
    whole = pd.read_csv(data / "big.csv")
    whole_peak = tracemalloc.get_traced_memory()[1]
    del whole
    tracemalloc.reset_peak()
    with session_scope(factory) as s:
        info = access.register(s, "kpi", "big", (data / "big.csv").as_uri(), kind="HISTORICAL",
                               timestamp_column="ts")
        dv = version_row(s, "kpi", "big")
        streamed = sum(1 for _ in access.iter_rows(s, dv))
        sampled = len(access.sample(s, dv))
    ref_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert info.row_count == streamed == rows, (info.row_count, streamed)
    assert sampled == 1_000, sampled
    assert ref_peak < whole_peak / 2, (
        f"reading by reference peaked at {ref_peak / 2**20:.1f} MiB, "
        f"loading whole at {whole_peak / 2**20:.1f} MiB"
    )
    return (f"{rows} rows ({size_mb:.1f} MiB CSV): register + stream + sample peaked at "
            f"{ref_peak / 2**20:.1f} MiB vs {whole_peak / 2**20:.1f} MiB to load it whole")


def same_hash_inline_or_reference(tmp: Path) -> str:
    import test_phase5_data_by_reference as t

    from oran_adapt.adapters.datasets import FileDataset
    from oran_adapt.datastore.access import DataAccess

    data = tmp / "data"
    data.mkdir()
    access = DataAccess([FileDataset([str(data)])], chunk_rows=3)
    for fmt in ("csv", "parquet", "jsonl"):
        sub = tmp / fmt
        sub.mkdir()
        factory = _factory(_settings(sub))
        t.test_reference_hashes_and_reads_like_inline(factory, access, data, fmt)
    snap = tmp / "snapshot"
    snap.mkdir()
    t.test_training_snapshot_of_referenced_data_is_derived(_factory(_settings(snap)), access,
                                                           data)
    return ("csv, parquet, jsonl: equal hashes, equal rows, no data_record rows; derived "
            "training snapshot hashes like its rows copied inline")


def refusals(tmp: Path) -> str:
    import test_phase5_data_by_reference as t
    from fastapi.testclient import TestClient

    from oran_adapt.adapters.datasets import FileDataset
    from oran_adapt.api.app import create_app
    from oran_adapt.datastore.access import DataAccess

    for name, run in (
        ("changed", lambda f, d: t.test_changed_object_is_refused_on_read_and_fails_verify(
            f, DataAccess([FileDataset([str(d)])], chunk_rows=3), d)),
        ("hash-mode", t.test_hash_mode_catches_a_change_the_fingerprint_misses),
        ("limits", t.test_limits_are_enforced_before_reading),
    ):
        sub = tmp / name
        (sub / "data").mkdir(parents=True)
        run(_factory(_settings(sub)), sub / "data")
    (tmp / "allow" / "data").mkdir(parents=True)
    t.test_uris_outside_the_allow_list_are_refused(tmp / "allow" / "data", tmp / "allow")

    api = tmp / "api"
    api.mkdir()
    api_data = api / "data"
    api_data.mkdir()
    configured = _settings(api).model_copy(update={"dataset_backends": "file",
                                                   "dataset_file_roots": [str(api_data)]})
    with TestClient(create_app(configured)) as client:
        t.test_api_registers_pages_and_verifies_by_reference(client, api_data)
    return ("changed source: refused on read (fingerprint, and hash mode), verify fails; "
            "source bytes / row ceiling / allow-list refused; API 409 / 422")


def conformance_everywhere(tmp: Path) -> str:
    import test_phase5_data_by_reference as t

    from oran_adapt import plugins

    names = ["file", "fsspec", "http", "gcs", "s3"]
    installed = set(plugins.adapters("dataset"))
    assert installed == set(names), f"adapters without a harness: {installed ^ set(names)}"
    server = ThreadingHTTPServer(("127.0.0.1", 0), t._Store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = f"127.0.0.1:{server.server_address[1]}"
        for name in names:
            sub = tmp / name
            sub.mkdir()
            t.test_dataset_adapter_conformance(name, sub, store)
        pinned = tmp / "pinned"
        pinned.mkdir()
        t.test_pinned_reference_survives_an_overwrite(_factory(_settings(pinned)), pinned,
                                                      store)
    finally:
        server.shutdown()
        server.server_close()
    return (f"{len(names)} adapters; s3/gcs also keep reading the pinned object version. "
            "HTTP/GCS against a local server, S3 against a client double: unverified against "
            "the real services")


def cdc_any_table(tmp: Path) -> str:
    import test_phase5_data_by_reference as t

    settings = _settings(tmp)
    t.test_trigger_sql_refuses_unsafe_identifiers()
    t.test_polling_cdc_follows_a_table_with_its_own_columns(settings, _factory(settings))
    t.test_debezium_messages_map_through_the_same_columns()
    return ("cell_kpi(sample_id, cell, ts, prb_util): triggers generated, 5 changes captured, "
            "materialized to 2 rows; Debezium mapped alike")


def boundaries_and_docs(tmp: Path) -> str:
    from test_import_boundary import violations

    from oran_adapt import plugins

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    path = ROOT / "docs" / "adapters" / "dataset.md"
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    guide = path.read_text(encoding="utf-8")
    undocumented = [a for a in plugins.adapters("dataset") if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/dataset.md: {undocumented}"
    return "import boundary clean; all dataset adapters documented"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("bounded memory for a large referenced object", bounded_memory),
    ("same rows, same hash, inline or by reference", same_hash_inline_or_reference),
    ("changed, oversized and disallowed sources refused", refusals),
    ("conformance suite green for every dataset adapter", conformance_everywhere),
    ("CDC from a table of any shape", cdc_any_table),
    ("boundaries and documentation", boundaries_and_docs),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase5-", ignore_cleanup_errors=True) as tmp:
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}")
            else:
                print(f"PASS  {name}\n      {detail}")
    print(f"\nphase 5 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
