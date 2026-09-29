# Dataset adapters (data by reference)

A data version is either **rows stored in the database** (sent inline as JSON records, a CSV
through the CLI, or folded from CDC events) or a **reference**: the URI of a Parquet, CSV or
JSON Lines object that stays where it is. A referenced version stores only metadata: the URI,
the exact object version it pinned, a fingerprint, the column types, the row count, the time
range and the content hash. Its rows are read in `DATASET_CHUNK_ROWS` batches whenever a job,
the analysis or the API needs them.

A dataset adapter implements `oran_adapt.ports.DatasetPort` for one kind of store.
`DATASET_BACKENDS` names the enabled adapters (comma-separated, or `none`: inline data only).
The core finds adapters through the `oran_adapt.dataset` entry-point group and builds them at
the composition root (`bootstrap.build_data_access`). `oran_adapt.datastore.access.DataAccess`
routes each URI to the adapter serving its scheme.

Shipped adapters:

| Adapter | Module | Reads | Pins / fingerprint | Needs | Verified |
|---|---|---|---|---|---|
| `file` | `adapters/datasets.py` | `file://` paths under `DATASET_FILE_ROOTS` | the path / size + mtime | `DATASET_FILE_ROOTS` | local |
| `http` | `adapters/datasets.py` | `https://` URLs on `DATASET_HTTP_ALLOWED_HOSTS` (plain `http://` with `DATASET_HTTP_ALLOW_PLAIN`) | the URL / ETag, else Last-Modified + length | `DATASET_HTTP_ALLOWED_HOSTS` | local server |
| `fsspec` | `adapters/datasets.py` | any fsspec filesystem (`memory`, `sftp`, `abfs`, `hf`...) under `DATASET_FSSPEC_PREFIXES` | the URI / the store's info fields (ETag, md5, mtime...) | `DATASET_FSSPEC_PREFIXES`; extra `[fsspec]` plus the filesystem's package | `memory://` locally; other filesystems unverified |
| `s3` | `adapters/datasets_cloud.py` | `s3://bucket/key` on `DATASET_S3_BUCKETS` | `?versionId=` on a versioned bucket / ETag | `DATASET_S3_BUCKETS`; extra `[aws]` | client double, unverified against S3 / MinIO |
| `gcs` | `adapters/datasets_cloud.py` | `gs://bucket/object` on `DATASET_GCS_BUCKETS`, over the JSON API (no SDK) | `?generation=` / generation + md5 | `DATASET_GCS_BUCKETS` | local server, unverified against GCS |

Parquet needs `pyarrow` (extra `[parquet]`). `GET /api/v1/capabilities` lists every adapter
and its keys.

## Registering and reading

```bash
# API: exactly one of records or storage_uri
curl -X POST .../api/v1/datasets/kpi/versions \
  -d '{"version": "v3", "storage_uri": "s3://kpi-bucket/2026/09/cells.parquet",
       "timestamp_column": "ts"}'
curl '.../api/v1/datasets/kpi/versions/v3/rows?offset=0&limit=50'
curl -X POST .../api/v1/datasets/kpi/versions/v3/verify

# CLI
python -m oran_adapt.cli data register --dataset kpi --version v3 \
    --uri s3://kpi-bucket/2026/09/cells.parquet --timestamp-column ts
python -m oran_adapt.cli data rows --dataset kpi --version v3 --limit 20
python -m oran_adapt.cli data verify --dataset kpi --version v3
```

* **Same rows, same hash.** Registering streams the object twice. The first pass fixes one
  type per column, since text formats infer types per batch. The second pass hashes the rows
  cast to those types, one row at a time (`formats.RowHasher`). The result equals the hash the
  same rows get when sent inline, whatever the batch size. Re-registering the same content
  under the same version name is idempotent (200). Different content under a taken name is a
  409.
* **Format** comes from the URI suffix (`.parquet`/`.pq`, `.csv`, `.jsonl`/`.ndjson`), or from
  `format` / `--format`.
* **Timestamps**: `timestamp_column` per version, default `DATASET_TIME_COLUMN`. Every row
  needs one. Referenced data has no "start + spacing" fallback.
* **Immutability.** Before a read, the object's fingerprint is compared with the registered
  one (`DATASET_VERIFY_ON_READ=fingerprint`). A mismatch is refused with
  `DATA_SOURCE_CHANGED` (409) and nothing is read. Where the store gives no fingerprint, or
  with `DATASET_VERIFY_ON_READ=hash`, the rows are re-hashed while they are read and the read
  fails at the end on a mismatch. File and fsspec fingerprints are size + modification time:
  a same-size rewrite within the clock's resolution can slip past them, so use `hash` for
  stores rewritten in place. `s3` (versioned buckets) and `gcs` read the pinned object version,
  so an overwrite of the key does not affect a registered version at all. `verify` always
  re-hashes.
* **Training snapshots** of referenced data are *derived* versions. They record the source
  version ids and the held-out row ids, not a copy of the rows, and they hash like those rows
  sent inline.

## Memory limits

| Key | Default | Meaning |
|---|---|---|
| `DATASET_CHUNK_ROWS` | `10000` | rows per batch read from an object or the database |
| `DATASET_MAX_ROWS` | `1000000` | rows a job may hold in memory. Checked from the recorded row counts *before* reading; refused with `DATA_TOO_LARGE` (413, `key=DATASET_MAX_ROWS`) |
| `DATASET_ANALYSIS_MAX_ROWS` | `100000` | drift analysis reads an evenly spread, deterministic sample of at most this many rows per version (`data_slices[].sampled`) |
| `DATASET_MAX_SOURCE_BYTES` | `4 GiB` | larger objects are refused before download |
| `DATASET_SPOOL_DIR` | system temp | where remote objects are spooled (deleted on close) |

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `DATASET_BACKENDS` | `none` | enabled adapters, comma-separated |
| `DATASET_TIME_COLUMN` | `observed_at` | default timestamp column |
| `DATASET_VERIFY_ON_READ` | `fingerprint` | `fingerprint`, `hash` or `off` |
| `DATASET_FILE_ROOTS` | `[]` | `file`: allowed directories (paths are resolved first, so `..` and symlinks cannot escape) |
| `DATASET_HTTP_ALLOWED_HOSTS` / `_ALLOW_PLAIN` / `_TIMEOUT_S` / `_TOKEN` | `[]` / `false` / `30` / unset | `http`. Redirects are not followed; credentials in URLs are refused |
| `DATASET_FSSPEC_PREFIXES` / `_OPTIONS` | `[]` / unset | `fsspec`: allowed URI prefixes; storage options as `{"<protocol>": {...}}` JSON (secret) |
| `DATASET_S3_BUCKETS` / `_REGION` / `_ENDPOINT_URL` | `[]` / unset / unset | `s3` |
| `DATASET_GCS_BUCKETS` / `_ENDPOINT` / `_CREDENTIALS` | `[]` / `https://storage.googleapis.com` / `adc` | `gcs` |

A URI no enabled adapter accepts is refused with `DATA_SOURCE_NOT_ALLOWED` (422). A missing
object is `DATASET_NOT_FOUND` (404), an unreachable store `DATA_SOURCE_UNAVAILABLE` (503), and
an unparsable object `DATA_FORMAT_INVALID` (422). Metrics: `dataset_rows_read_total{storage}`,
`dataset_bytes_read_total{scheme}`, `dataset_read_duration_seconds{storage}`,
`dataset_read_refused_total{reason}`.

## Writing a new adapter

1. Declare `schemes`. Implement `check(uri)`: refuse anything outside the configured allow-list
   with `DataSourceNotAllowedError`, without I/O. Implement `stat(uri) -> SourceStat` (the
   pinned URI, a fingerprint that changes when the content does, the size when known) and
   `open(uri) -> BinaryIO` (seekable; spool remote bodies with `adapters.datasets.spool`,
   which enforces `DATASET_MAX_SOURCE_BYTES`).
2. Map failures: a missing object raises `DatasetNotFoundError`, an unreachable store
   `DataSourceUnavailableError`, and a refusal `DataSourceNotAllowedError`. No SDK exception
   may escape.
3. Import vendor SDKs inside the factory only, under `oran_adapt/adapters/`, and list them in
   `SDK_HOMES` in `tests/unit/test_import_boundary.py`.
4. Declare an `AdapterSpec` and register it under
   `[project.entry-points."oran_adapt.dataset"]`.
5. Run the conformance suite and add the adapter to the table above.

## Conformance suite

`oran_adapt.conformance.dataset` checks an adapter against a `Context` that can `put(name,
data)` an object into the store and names a `missing(name)` URI and an `outside` URI:

* `protocol`;
* `stat`, which must be stable for an unchanged object and give the right size;
* `open_reads_bytes`, which reads the exact bytes and must be seekable;
* `change_detected`, where an overwrite changes the fingerprint or the pinned URI;
* `missing`, which must raise `DatasetNotFoundError`;
* `refuses_outside`, where `check`, `stat` and `open` must raise `DataSourceNotAllowedError`;
* `rows_stable`, where CSV and JSONL read through `DataAccess` hash the same at batch sizes 1,
  2 and 1000, and the same as inline;
* with `pinned=True`, `pinned_stable`, where the pinned URI still reads the old bytes after an
  overwrite.

```python
from oran_adapt.conformance.dataset import Context, run
run(MyDataset(...), Context(put=my_put, missing=my_missing, outside="s3://other/x.csv"))
```

`tests/unit/test_phase5_data_by_reference.py` runs it for every shipped adapter.

## CDC from any table

The polling and Kafka CDC sources no longer assume kpi_sample's shape. `CDC_KEY_COLUMN`,
`CDC_DATASET_COLUMN` (or a fixed `CDC_DATASET_ID`), `CDC_TIME_COLUMN` and
`CDC_PAYLOAD_COLUMN` map the source row. `CDC_PAYLOAD_COLUMN=""` takes every other column as
the features of a plain wide table. For the polling source, generate the changelog triggers
with the command below, review them, and apply them:

```bash
python -m oran_adapt.cli cdc trigger-sql --table cell_kpi --dialect sqlite \
    --key-column sample_id --columns sample_id,cell,ts,prb_util
python -m oran_adapt.cli cdc trigger-sql --table cell_kpi --dialect postgresql
```
