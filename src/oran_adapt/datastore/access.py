"""Reading data versions wherever their rows live, in bounded batches.

A data version stores its rows in one of three ways (``versioning.storage_of``):

* ``rows`` - data_record rows in this database (uploads, CDC windows, CurrentData);
* ``reference`` - an object in storage the framework does not own (a file, an HTTPS URL, an
  S3 or GCS object, any fsspec filesystem), read through a dataset adapter (port
  ``dataset``, enabled by DATASET_BACKENDS). The version records the object's pinned URI and
  fingerprint, and reading refuses an object that changed since it was registered;
* ``derived`` - the rows of other versions minus excluded rows (a training snapshot whose
  sources include referenced data; copying them into the database would defeat the point).

``DataAccess`` hides the difference. Every reader gets the same ``Row`` shape (the attributes
of a DataRecord). Rows that are not database rows get stable negative ids
(``external_row_id``), so they never collide with data_record ids and the same row gets the
same id on every read.

Memory is bounded twice. Objects stream DATASET_CHUNK_ROWS rows at a time, and anything that
must be held in memory whole (training rows, derived versions) is refused up front, from the
recorded row counts, when it would pass DATASET_MAX_ROWS: a DataTooLargeError naming the key,
never an out-of-memory crash. Analysis reads at most DATASET_ANALYSIS_MAX_ROWS rows per
version, a deterministic systematic sample of the whole version.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core import metrics
from oran_adapt.core.enums import AssociationRole, DataKind
from oran_adapt.core.errors import (
    ArtifactError,
    DatasetNotFoundError,
    DataSourceChangedError,
    DataSourceNotAllowedError,
    DataTooLargeError,
)
from oran_adapt.datastore.formats import (
    RowHasher,
    canonical_rows,
    conform,
    infer_format,
    iter_frames,
    split_timestamps,
    unify_dtypes,
)
from oran_adapt.datastore.versioning import (
    STORAGE_DERIVED,
    STORAGE_REFERENCE,
    STORAGE_ROWS,
    VersionInfo,
    create_stored_version,
    keyed_hash,
    storage_of,
)
from oran_adapt.db.models import DataRecord, DataVersion

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DatasetPort, SourceStat

ROW_ID_STRIDE = 2**40  # rows per version an external row id can number


def external_row_id(data_version_id: int, index: int) -> int:
    """The id of row ``index`` (0-based, in stored order) of a version whose rows are not
    data_record rows: negative, unique across versions, the same on every read."""
    return -(data_version_id * ROW_ID_STRIDE + index + 1)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


@dataclass(frozen=True)
class Row:
    """One data row, whatever stores it (the attributes of a DataRecord)."""

    id: int
    data_version_id: int
    observed_at: datetime  # always UTC-aware
    payload: dict
    record_key: str | None = None


@dataclass
class ReferenceScan:
    """What registering an object by reference records about it."""

    uri: str
    pinned_uri: str
    fingerprint: str | None
    size_bytes: int | None
    format: str
    timestamp_column: str
    columns: dict[str, str]
    row_count: int
    data_start: datetime
    data_end: datetime
    content_hash: str
    extra: dict = field(default_factory=dict)

    def reference(self) -> dict:
        return {
            "uri": self.uri,
            "pinned_uri": self.pinned_uri,
            "fingerprint": self.fingerprint,
            "size_bytes": self.size_bytes,
            "format": self.format,
            "timestamp_column": self.timestamp_column,
        }


class DataAccess:
    """Row access for every data version. ``backends`` are the enabled dataset adapters
    (bootstrap.build_data_access); without any, only database-stored versions can be read."""

    def __init__(
        self,
        backends: Sequence[DatasetPort] = (),
        *,
        chunk_rows: int = 10_000,
        max_rows: int = 1_000_000,
        analysis_max_rows: int = 100_000,
        max_source_bytes: int = 4 * 1024**3,
        time_column: str = "observed_at",
        verify_on_read: str = "fingerprint",
    ) -> None:
        self.backends = list(backends)
        self.chunk_rows = chunk_rows
        self.max_rows = max_rows
        self.analysis_max_rows = analysis_max_rows
        self.max_source_bytes = max_source_bytes
        self.time_column = time_column
        self.verify_on_read = verify_on_read

    @classmethod
    def from_settings(
        cls, settings: Settings, backends: Sequence[DatasetPort] = ()
    ) -> DataAccess:
        return cls(
            backends,
            chunk_rows=settings.dataset_chunk_rows,
            max_rows=settings.dataset_max_rows,
            analysis_max_rows=settings.dataset_analysis_max_rows,
            max_source_bytes=settings.dataset_max_source_bytes,
            time_column=settings.dataset_time_column,
            verify_on_read=settings.dataset_verify_on_read,
        )

    # ------------------------------------------------------------------ adapters
    def backend_for(self, uri: str) -> DatasetPort:
        """The enabled adapter that may read ``uri``: its scheme, inside its allow-list."""
        scheme = urlsplit(uri).scheme.lower()
        refusal: DataSourceNotAllowedError | None = None
        for backend in self.backends:
            if scheme not in backend.schemes:
                continue
            try:
                backend.check(uri)
            except DataSourceNotAllowedError as exc:
                refusal = exc
                continue
            return backend
        metrics.DATASET_READ_REFUSED.labels(reason="not_allowed").inc()
        if refusal is not None:
            raise refusal
        raise DataSourceNotAllowedError(
            f"no enabled dataset adapter reads {scheme or 'scheme-less'} URIs "
            "(see DATASET_BACKENDS)",
            scheme=scheme,
            enabled=sorted({s for b in self.backends for s in b.schemes}),
        )

    def _stat(self, backend: DatasetPort, uri: str) -> SourceStat:
        stat = backend.stat(uri)
        if stat.size_bytes is not None and stat.size_bytes > self.max_source_bytes:
            metrics.DATASET_READ_REFUSED.labels(reason="too_large").inc()
            raise DataTooLargeError(
                "the referenced object is larger than DATASET_MAX_SOURCE_BYTES",
                key="DATASET_MAX_SOURCE_BYTES",
                limit=self.max_source_bytes,
                size_bytes=stat.size_bytes,
            )
        return stat

    def _frames(
        self, backend: DatasetPort, uri: str, fmt: str, size: int | None
    ) -> Iterator[pd.DataFrame]:
        with backend.open(uri) as fh:
            yield from iter_frames(fh, fmt, self.chunk_rows)
        if size:
            metrics.DATASET_BYTES_READ.labels(scheme=urlsplit(uri).scheme.lower()).inc(size)

    # ------------------------------------------------------------------ registering
    def scan(
        self, uri: str, *, fmt: str | None = None, timestamp_column: str | None = None
    ) -> ReferenceScan:
        """Stream the object twice (column types first, then rows cast to them) and describe
        it: row count, time range, column types and the content hash its rows would have if
        they were sent inline. Nothing is held beyond one batch."""
        backend = self.backend_for(uri)
        stat = self._stat(backend, uri)
        fmt = infer_format(uri, fmt)
        column = timestamp_column or self.time_column

        columns: dict[str, str] = {}
        for frame in self._frames(backend, stat.pinned_uri, fmt, None):
            payload, _ = split_timestamps(frame, column)
            columns = unify_dtypes(columns, payload)

        hasher = RowHasher()
        start: datetime | None = None
        end: datetime | None = None
        for frame in self._frames(backend, stat.pinned_uri, fmt, stat.size_bytes):
            payload, stamps = split_timestamps(frame, column)
            hasher.add_frame(conform(payload, columns), stamps)
            if stamps:
                start = min(stamps) if start is None else min(start, *stamps)
                end = max(stamps) if end is None else max(end, *stamps)
        if not hasher.rows or start is None or end is None:
            raise ArtifactError("refusing to register an empty data object", uri=uri)
        return ReferenceScan(
            uri=uri,
            pinned_uri=stat.pinned_uri,
            fingerprint=stat.fingerprint,
            size_bytes=stat.size_bytes,
            format=fmt,
            timestamp_column=column,
            columns=columns,
            row_count=hasher.rows,
            data_start=start,
            data_end=end,
            content_hash=hasher.hexdigest(),
        )

    def register(
        self,
        session: Session,
        dataset_id: str,
        version: str,
        uri: str,
        *,
        kind: DataKind | str,
        fmt: str | None = None,
        timestamp_column: str | None = None,
        parent_version: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        role: AssociationRole | str | None = None,
        source: str | None = None,
        dataset_name: str | None = None,
        actor: str = "system",
    ) -> VersionInfo:
        """Register the object at ``uri`` as data version ``version`` without copying its
        rows. Same idempotency and conflict rules as an inline upload, and the same content
        hash for the same rows."""
        scan = self.scan(uri, fmt=fmt, timestamp_column=timestamp_column)
        return create_stored_version(
            session,
            dataset_id,
            version,
            digest=scan.content_hash,
            row_count=scan.row_count,
            columns=scan.columns,
            data_start=scan.data_start,
            data_end=scan.data_end,
            kind=kind,
            storage=STORAGE_REFERENCE,
            storage_uri=uri,
            extra={"reference": scan.reference()},
            parent_version=parent_version,
            model_id=model_id,
            model_version=model_version,
            role=role,
            source=source or f"registered by reference: {uri}",
            dataset_name=dataset_name,
            actor=actor,
        )

    # ------------------------------------------------------------------ reading
    def iter_rows(self, session: Session, dv: DataVersion) -> Iterator[Row]:
        """The version's rows, streamed: database rows by (observed_at, id), a referenced
        object in its own row order, a derived version by (observed_at, source row id)."""
        storage = storage_of(dv)
        started = time.monotonic()
        count = 0
        if storage == STORAGE_REFERENCE:
            rows = self._iter_reference(dv)
        elif storage == STORAGE_DERIVED:
            rows = iter(self._derived_rows(session, dv))
        else:
            rows = self._iter_db(session, dv)
        for row in rows:
            count += 1
            yield row
        metrics.DATASET_ROWS_READ.labels(storage=storage).inc(count)
        metrics.DATASET_READ_DURATION.labels(storage=storage).observe(
            time.monotonic() - started
        )

    def _iter_db(self, session: Session, dv: DataVersion) -> Iterator[Row]:
        query = (
            select(DataRecord)
            .where(DataRecord.data_version_id == dv.id)
            .order_by(DataRecord.observed_at, DataRecord.id)
            .execution_options(yield_per=self.chunk_rows)
        )
        for r in session.scalars(query):
            yield Row(r.id, r.data_version_id, _utc(r.observed_at), r.payload, r.record_key)

    def _check_unchanged(self, backend: DatasetPort, dv: DataVersion, ref: dict) -> bool:
        """Compare the object's fingerprint with the registered one. Returns whether the rows
        must also be re-hashed (no fingerprint to compare, or DATASET_VERIFY_ON_READ=hash)."""
        if self.verify_on_read == "off":
            return False
        if ref.get("fingerprint"):
            stat = self._stat(backend, ref["pinned_uri"])
            if stat.fingerprint != ref["fingerprint"]:
                metrics.DATASET_READ_REFUSED.labels(reason="changed").inc()
                raise DataSourceChangedError(
                    "the object behind this data version changed since it was registered",
                    data_version_id=dv.id,
                    uri=ref["uri"],
                    registered=ref["fingerprint"],
                    found=stat.fingerprint,
                )
            return self.verify_on_read == "hash"
        return True

    def _iter_reference(self, dv: DataVersion, *, rehash: bool | None = None) -> Iterator[Row]:
        extra = dv.extra or {}
        ref = extra["reference"]
        columns = extra["columns"]
        backend = self.backend_for(ref["uri"])
        must_hash = self._check_unchanged(backend, dv, ref) if rehash is None else rehash
        hasher = RowHasher() if must_hash else None
        index = 0
        for frame in self._frames(backend, ref["pinned_uri"], ref["format"], ref["size_bytes"]):
            payload, stamps = split_timestamps(frame, ref["timestamp_column"])
            for row, ts in zip(canonical_rows(conform(payload, columns), stamps), stamps,
                               strict=True):
                if hasher is not None:
                    hasher.add(row)
                row.pop("observed_at")
                yield Row(external_row_id(dv.id, index), dv.id, ts, row)
                index += 1
        if hasher is not None and hasher.hexdigest() != dv.content_hash:
            metrics.DATASET_READ_REFUSED.labels(reason="changed").inc()
            raise DataSourceChangedError(
                "the rows behind this data version changed since it was registered",
                data_version_id=dv.id,
                uri=ref["uri"],
                registered=dv.content_hash,
                found=hasher.hexdigest(),
            )

    def _derived_rows(self, session: Session, dv: DataVersion) -> list[Row]:
        spec = (dv.extra or {})["derived"]
        excluded = set(spec.get("excluded_row_ids", []))
        rows = [
            r for r in self.load_records(session, spec["sources"], require=False)
            if r.id not in excluded
        ]
        return [
            Row(external_row_id(dv.id, i), dv.id, r.observed_at, r.payload, r.record_key)
            for i, r in enumerate(rows)
        ]

    def _versions(self, session: Session, ids: Sequence[int]) -> list[DataVersion]:
        versions = [session.get(DataVersion, i) for i in ids]
        missing = [i for i, v in zip(ids, versions, strict=True) if v is None]
        if missing:
            raise DatasetNotFoundError("unknown data version", data_version_ids=missing)
        return [v for v in versions if v is not None]

    def _check_ceiling(self, rows: int, **context: object) -> None:
        if rows > self.max_rows:
            metrics.DATASET_READ_REFUSED.labels(reason="too_large").inc()
            raise DataTooLargeError(
                f"{rows} rows would have to be held in memory, more than DATASET_MAX_ROWS "
                f"({self.max_rows}); raise the limit or use smaller data versions",
                key="DATASET_MAX_ROWS",
                limit=self.max_rows,
                rows=rows,
                **context,
            )

    def load_records(
        self, session: Session, data_version_ids: Sequence[int], *, require: bool = True
    ) -> list[Row]:
        """Every row of the given versions, oldest first (id breaks timestamp ties). Refused
        before reading anything when the recorded row counts pass DATASET_MAX_ROWS."""
        versions = self._versions(session, data_version_ids)
        self._check_ceiling(
            sum(v.row_count or 0 for v in versions), data_version_ids=list(data_version_ids)
        )
        rows = [row for v in versions for row in self.iter_rows(session, v)]
        if require and not rows:
            raise ArtifactError(
                "no data records found for data versions",
                data_version_ids=list(data_version_ids),
            )
        rows.sort(key=lambda r: (r.observed_at, r.id))
        return rows

    def frame(self, session: Session, data_version_id: int) -> pd.DataFrame:
        """One version's payload columns as a DataFrame (within DATASET_MAX_ROWS)."""
        rows = self.load_records(session, [data_version_id], require=False)
        if not rows:
            raise ArtifactError(
                "no data records found for data version", data_version_id=data_version_id
            )
        return pd.DataFrame([r.payload for r in rows])

    def sample(self, session: Session, dv: DataVersion) -> list[Row]:
        """At most DATASET_ANALYSIS_MAX_ROWS rows spread evenly over the whole version (every
        k-th row, the same rows on every read), in stored order."""
        total = dv.row_count or 0
        limit = self.analysis_max_rows
        if total <= limit:
            return list(self.iter_rows(session, dv))
        wanted = (k * total // limit for k in range(limit))
        target = next(wanted)
        picked: list[Row] = []
        for index, row in enumerate(self.iter_rows(session, dv)):
            if index == target:
                picked.append(row)
                target = next(wanted, -1)
                if target < 0:
                    break
        return picked

    def page(self, session: Session, dv: DataVersion, *, offset: int, limit: int) -> list[Row]:
        """Rows ``offset`` .. ``offset + limit`` of the version, streamed to that point."""
        if storage_of(dv) == STORAGE_ROWS:
            query = (
                select(DataRecord)
                .where(DataRecord.data_version_id == dv.id)
                .order_by(DataRecord.observed_at, DataRecord.id)
                .offset(offset)
                .limit(limit)
            )
            return [
                Row(r.id, r.data_version_id, _utc(r.observed_at), r.payload, r.record_key)
                for r in session.scalars(query)
            ]
        out: list[Row] = []
        for index, row in enumerate(self.iter_rows(session, dv)):
            if index >= offset + limit:
                break
            if index >= offset:
                out.append(row)
        return out

    def verify(self, session: Session, dv: DataVersion) -> dict:
        """Recompute the version's content hash from its rows as they are stored now."""
        storage = storage_of(dv)
        hasher = RowHasher()
        keys: list[str | None] = []
        if storage == STORAGE_REFERENCE:
            ref = (dv.extra or {})["reference"]
            stat = self._stat(self.backend_for(ref["uri"]), ref["pinned_uri"])
            rows: Iterator[Row] | list[Row] = self._iter_reference(dv, rehash=False)
            fingerprint_ok = not ref.get("fingerprint") or stat.fingerprint == ref["fingerprint"]
        elif storage == STORAGE_ROWS:
            # Hashed at ingest in the order the rows were sent, which is id order.
            query = (
                select(DataRecord)
                .where(DataRecord.data_version_id == dv.id)
                .order_by(DataRecord.id)
                .execution_options(yield_per=self.chunk_rows)
            )
            rows = (
                Row(r.id, r.data_version_id, _utc(r.observed_at), r.payload, r.record_key)
                for r in session.scalars(query)
            )
            fingerprint_ok = True
        else:
            rows = self.iter_rows(session, dv)
            fingerprint_ok = True
        for r in rows:
            hasher.add({"observed_at": r.observed_at.isoformat(),
                        **{k: r.payload[k] for k in sorted(r.payload)}})
            keys.append(r.record_key)
        digest = hasher.hexdigest()
        if storage == STORAGE_ROWS:
            deleted = (dv.extra or {}).get("deleted_keys")
            digest = keyed_hash(digest, keys if any(k is not None for k in keys) else None,
                                deleted)
        return {
            "data_version_id": dv.id,
            "version": dv.version,
            "storage": storage,
            "rows": hasher.rows,
            "content_hash": dv.content_hash,
            "recomputed_hash": digest,
            "fingerprint_matches": fingerprint_ok,
            "matches": digest == dv.content_hash and fingerprint_ok,
        }
