# Hardening Phase 5 report: data access by reference

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. No real object store
was used:
- `file` and `fsspec` (`memory://`) were exercised for real;
- `http` and `gcs` ran against a local HTTP server (`_Store` in
  `tests/unit/test_phase5_data_by_reference.py`, which also serves the GCS JSON API paths);
- `s3` ran against a client double.

**`http`, `s3`, `gcs` and fsspec filesystems other than `memory://` are unverified against
real stores.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 4: data arrives only as JSON rows in the request body and is stored one DB row per record | `POST /datasets/{id}/versions` takes exactly one of `records` or `storage_uri` (+ `format`, `timestamp_column`). A referenced version stores only metadata (URI, pinned object version, fingerprint, column types, row count, time range, content hash) in `data_version`; no `data_record` rows. Parquet, CSV and JSON Lines (`datastore/formats.py`). CLI `data register` |
| Whole versions loaded into memory (`.all()`) | `datastore/access.DataAccess` streams every storage kind in `DATASET_CHUNK_ROWS` batches (`iter_rows`). Jobs check `DATASET_MAX_ROWS` from the recorded row counts **before** reading (`DATA_TOO_LARGE`, 413). Drift analysis reads a deterministic, evenly spread sample of at most `DATASET_ANALYSIS_MAX_ROWS` rows per version. Objects over `DATASET_MAX_SOURCE_BYTES` are refused before download |
| Content hash computed over the whole JSON in memory | `formats.RowHasher` hashes one row at a time after a first pass fixes one type per column. The hash equals the inline hash of the same rows at any batch size, so a version's identity does not depend on how it arrived |
| A reference can change under a version | fingerprint check before each read (`DATA_SOURCE_CHANGED`, 409); `DATASET_VERIFY_ON_READ=hash` re-hashes during the read. `s3` (versioned buckets) and `gcs` read the pinned `versionId` / `generation`. `POST .../verify` and CLI `data verify` re-hash on demand |
| Training snapshots copied the rows | snapshots of referenced data are `derived` versions: the source version ids plus the held-out row ids. They hash like the rows copied inline |
| Time column always `observed_at` | `timestamp_column` per version, default `DATASET_TIME_COLUMN` |
| CDC reads only `kpi_sample` with fixed columns | `cdc.events.CdcRowMapping` (`CDC_KEY_COLUMN`, `CDC_DATASET_COLUMN` / `CDC_DATASET_ID`, `CDC_TIME_COLUMN`, `CDC_PAYLOAD_COLUMN`; empty payload column = wide table), used by the polling and Kafka (Debezium) sources. `oran-adapt cdc trigger-sql` generates changelog triggers for any table (SQLite, PostgreSQL) for an operator to review |
| No way to read a version's rows back | `GET /datasets/{id}/versions/{version}/rows?offset&limit` and CLI `data rows`, the same for every storage kind |
| `dataset` port declared without an adapter | five adapters behind `oran_adapt.dataset` entry points; conformance suite `oran_adapt.conformance.dataset`; guide `docs/adapters/dataset.md` |

No migration: the storage kind and reference metadata live in `data_version.extra` and the
existing `storage_uri` column.

## 2. Ports and adapters

`DatasetPort` (`ports/runtime.py`) has these members:
- `schemes`;
- `check(uri)`, which runs the allow-list only, with no I/O;
- `stat(uri) -> SourceStat` (pinned URI, fingerprint, size);
- `open(uri) -> BinaryIO`, which must return a seekable file.

| Adapter | Reads | Pins / fingerprint | Test double |
|---|---|---|---|
| `file` | `file://` under `DATASET_FILE_ROOTS` (resolved, so `..` and symlinks cannot escape) | path / size + mtime | real files |
| `http` | `https://` on `DATASET_HTTP_ALLOWED_HOSTS`; no redirects, no credentials in URLs | URL / ETag, else Last-Modified + length | local server |
| `fsspec` | any fsspec filesystem under `DATASET_FSSPEC_PREFIXES` | URI / info fields | real `memory://` |
| `s3` | `s3://` on `DATASET_S3_BUCKETS` (needs `boto3`) | `?versionId=` / ETag | client double |
| `gcs` | `gs://` on `DATASET_GCS_BUCKETS`, JSON API over httpx (no SDK) | `?generation=` / generation + md5 | local server |

`boto3`/`botocore` are allowed in `adapters/datasets_cloud.py` (import-boundary test).
`fsspec` and `pyarrow` are optional libraries imported lazily where they are used. A missing
one fails with a `ConfigurationError` naming the extra.

New metrics:
- `dataset_rows_read_total{storage}`;
- `dataset_bytes_read_total{scheme}`;
- `dataset_read_duration_seconds{storage}`;
- `dataset_read_refused_total{reason}`.

New errors:
- `DATA_SOURCE_NOT_ALLOWED` (422);
- `DATA_SOURCE_UNAVAILABLE` (503);
- `DATA_SOURCE_CHANGED` (409);
- `DATA_FORMAT_INVALID` (422);
- `DATA_TOO_LARGE` (413).

## 3. Configuration keys added in Phase 5

Common:

| Key | Default |
|---|---|
| `DATASET_BACKENDS` | `none` |
| `DATASET_CHUNK_ROWS` | 10000 |
| `DATASET_MAX_ROWS` | 1000000 |
| `DATASET_ANALYSIS_MAX_ROWS` | 100000 |
| `DATASET_MAX_SOURCE_BYTES` | 4 GiB |
| `DATASET_TIME_COLUMN` | `observed_at` |
| `DATASET_VERIFY_ON_READ` | `fingerprint` (also `hash` or `off`) |
| `DATASET_SPOOL_DIR` | unset |

Per adapter:

| Adapter | Keys |
|---|---|
| `file` | `DATASET_FILE_ROOTS` |
| `http` | `DATASET_HTTP_ALLOWED_HOSTS`, `DATASET_HTTP_ALLOW_PLAIN`, `DATASET_HTTP_TIMEOUT_S`, `DATASET_HTTP_TOKEN` (secret) |
| `fsspec` | `DATASET_FSSPEC_PREFIXES`, `DATASET_FSSPEC_OPTIONS` (secret) |
| `s3` | `DATASET_S3_BUCKETS`, `DATASET_S3_REGION`, `DATASET_S3_ENDPOINT_URL` |
| `gcs` | `DATASET_GCS_BUCKETS`, `DATASET_GCS_ENDPOINT`, `DATASET_GCS_CREDENTIALS` (`adc` or `none`) |

CDC mapping:

| Key | Default |
|---|---|
| `CDC_KEY_COLUMN` | `id` |
| `CDC_DATASET_COLUMN` | `dataset_id` |
| `CDC_DATASET_ID` | unset |
| `CDC_TIME_COLUMN` | `observed_at` |
| `CDC_PAYLOAD_COLUMN` | `payload` |

The package extras are:
- `[aws]`, which provides `boto3`;
- `[fsspec]`;
- `[parquet]`, which provides `pyarrow`.

With the defaults, no adapter is enabled and data is sent inline exactly as before.

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | A large referenced object is registered, streamed and sampled with bounded memory | PASS | acceptance check 1: a 100 000-row CSV (4.1 MiB). Register, stream and sample peaked at 5.4 MiB (tracemalloc), against 13.4 MiB to load it whole with pandas |
| 2 | Same rows, same hash, whether inline or by reference (CSV, Parquet, JSON Lines); nothing is copied into `data_record` | PASS | `test_reference_hashes_and_reads_like_inline[csv/parquet/jsonl]`; acceptance check 2 |
| 3 | Training snapshot of referenced data is a derived version that verifies | PASS | `test_training_snapshot_of_referenced_data_is_derived` |
| 4 | Changed source refused on read (by fingerprint, and by hash mode where the fingerprint misses it); verify fails | PASS | `test_changed_object_is_refused_on_read_and_fails_verify`, `test_hash_mode_catches_a_change_the_fingerprint_misses` |
| 5 | Row ceiling and source-size ceiling are enforced before reading; URIs outside the allow-list are refused | PASS | `test_limits_are_enforced_before_reading`, `test_uris_outside_the_allow_list_are_refused` |
| 6 | API: register by reference, page rows, verify; the API answers 409 or 422 on refusals | PASS | `test_api_registers_pages_and_verifies_by_reference`; acceptance check 3 |
| 7 | Every dataset adapter passes the conformance suite; pinned stores keep reading the registered bytes after an overwrite | PASS (local server/double) | `test_dataset_adapter_conformance[file/fsspec/http/gcs/s3]`, `test_pinned_reference_survives_an_overwrite`; acceptance check 4 |
| 8 | CDC follows a table with its own column names; Debezium maps through the same columns; unsafe identifiers are refused | PASS | `test_polling_cdc_follows_a_table_with_its_own_columns` (`cell_kpi(sample_id, cell, ts, prb_util)`: 5 changes captured, 2 rows materialized), `test_debezium_messages_map_through_the_same_columns`, `test_trigger_sql_refuses_unsafe_identifiers`; acceptance check 5 |
| 9 | Drift analysis samples large versions deterministically | PASS | `test_analysis_sample_is_bounded_and_spread` (20 rows sampled to 5) |
| 10 | Vendor SDKs only inside adapters; every adapter documented | PASS | `test_import_boundary.py`; acceptance check 6 |
| 11 | CLI `data register`, `data rows`, `data verify`, `cdc trigger-sql` | PASS | `test_cli_register_verify_and_trigger_sql` |

## 5. Hardcoding

**Removed:**
- the `kpi_sample` column shape in CDC;
- `observed_at` as the only time column for external data;
- the whole-version loads.

A24 is now only a default.

**Kept on purpose** (see `docs/hardcoding-inventory.md`, "Hardening Phase 5 status"):
- the format suffixes;
- the DDL identifier pattern;
- 1000 rows per page;
- the 500-character `storage_uri` column.

**Remaining:** C10 (`CDC_KAFKA_TOPIC` still defaults to the kpi_sample topic). Inventory
burn-down: C open 8 → 8.

## 6. Assumptions and defaults

These are recorded in `docs/OPEN-QUESTIONS.md` ("Dataset adapters"):
- **No adapter is enabled by default**, and every adapter reads only inside an explicit allow-list.
- **A registered object must not change.** Fingerprints are best effort for `file` and `fsspec`, where a same-size rewrite within the clock's resolution can be missed. `DATASET_VERIFY_ON_READ=hash` closes that gap at the cost of a full re-read.
- **S3 pins only on versioned buckets.** On an unversioned bucket, an overwrite is detected by ETag and refused.
- **Limits:** 1 000 000 rows per job, a sample of 100 000 rows for analysis, and 4 GiB per object.
- **Referenced data needs a timestamp column.**
- **CDC triggers are generated for review, never applied automatically.**

## 7. Unverified locally

- **`http`, `s3` and `gcs` against real services.** AWS S3, MinIO, Google Cloud Storage and a
  real HTTPS object host were not available, and `boto3` is not installed. The local server
  and the S3 double implement the calls as the adapters make them. They are not recordings of
  the real services. GCS authentication with application default credentials (`adc`) was not
  exercised; the local server ran with `none`.
- **fsspec filesystems other than `memory://`** (sftp, abfs, hf, ...). Their fingerprints depend
  on what each filesystem's `info()` returns.
- **The PostgreSQL trigger SQL.** It was only generated, and checked for content and refusals;
  it was never applied to a PostgreSQL database. The SQLite triggers were applied and
  exercised.
- **Debezium → Kafka for a table other than `kpi_sample`.** Only the message mapping was
  tested, with synthetic envelopes.
- **Reading referenced data from several API replicas at once.** This was not exercised. Each
  read is independent and stateless apart from the spool file.

## 8. Gate

`scripts/verify.sh 5`: **PASS in 157 s**, run locally on 2026-09-29. It covers:
- ruff;
- mypy, with 0 errors in 142 source files;
- the import boundary;
- the no-gaps lint;
- the scoped tests (datastore, cdc, analysis, adaptation: 227 passed);
- the smoke tier (196 passed), with 2 workers;
- acceptance, 6/6.
