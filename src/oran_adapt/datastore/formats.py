"""Reading tabular data in bounded batches, and hashing it without holding it.

Referenced data versions are Parquet, CSV or JSON Lines objects. ``iter_frames`` yields them
``batch_rows`` rows at a time, so memory use depends on the batch size, not the object size.
Text formats infer column types per batch, and a column can read as integers in one batch and
as floats in the next. ``unify_dtypes`` folds the per-batch types into one type per column, and
``conform`` casts every batch to it, so the rows (and the content hash) do not depend on where
the batch boundaries fall.

``RowHasher`` computes the same SHA-256 as hashing the whole canonical row list in one
``json.dumps`` call (``datastore.versioning.content_hash``), one row at a time. Data sent inline
and the same data registered by reference therefore get the same content hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import BinaryIO
from urllib.parse import urlsplit

import pandas as pd

from oran_adapt.core.errors import ConfigurationError, DataFormatError

FORMATS = ("parquet", "csv", "jsonl")
_SUFFIXES = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".csv": "csv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}


def infer_format(uri: str, declared: str | None = None) -> str:
    """``declared`` when given, else the format the URI path's suffix names."""
    if declared:
        if declared not in FORMATS:
            raise DataFormatError(
                f"unknown data format {declared!r}", supported=list(FORMATS)
            )
        return declared
    path = urlsplit(uri).path.lower()
    for suffix, fmt in _SUFFIXES.items():
        if path.endswith(suffix):
            return fmt
    raise DataFormatError(
        "cannot tell the data format from the URI; pass the format explicitly",
        supported=list(FORMATS),
    )


def iter_frames(fh: BinaryIO, fmt: str, batch_rows: int) -> Iterator[pd.DataFrame]:
    """The object's rows, at most ``batch_rows`` per frame, in stored order."""
    try:
        if fmt == "parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise ConfigurationError(
                    "reading Parquet needs pyarrow (pip install 'oran-adapt[parquet]')",
                    key="format",
                ) from exc

            for batch in pq.ParquetFile(fh).iter_batches(batch_size=batch_rows):
                yield batch.to_pandas()
        elif fmt == "csv":
            yield from pd.read_csv(fh, chunksize=batch_rows)
        elif fmt == "jsonl":
            yield from pd.read_json(fh, lines=True, chunksize=batch_rows)
        else:
            raise DataFormatError(f"unknown data format {fmt!r}", supported=list(FORMATS))
    except DataFormatError:
        raise
    except (ValueError, OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise DataFormatError(
            f"data is not valid {fmt}: {type(exc).__name__}: {exc}"[:500], format=fmt
        ) from exc
    except Exception as exc:  # pyarrow raises its own ArrowInvalid family
        if type(exc).__module__.startswith("pyarrow"):
            raise DataFormatError(
                f"data is not valid {fmt}: {exc}"[:500], format=fmt
            ) from exc
        raise


def _merge(a: str, b: str) -> str:
    if a == b:
        return a
    numeric = {"int64", "float64", "int32", "float32", "bool"}
    if {a, b} <= numeric - {"bool"}:
        return "float64"
    return "object"


def unify_dtypes(seen: Mapping[str, str], frame: pd.DataFrame) -> dict[str, str]:
    """``seen`` (column -> dtype so far) widened by ``frame``'s column types. Every batch must
    have the same columns: a batch that adds or drops one is a DataFormatError."""
    current = {str(c): str(t) for c, t in frame.dtypes.items()}
    if seen and set(seen) != set(current):
        raise DataFormatError(
            "columns differ between parts of the data",
            expected=sorted(seen),
            found=sorted(current),
        )
    return {c: _merge(seen[c], t) if c in seen else t for c, t in current.items()}


def conform(frame: pd.DataFrame, columns: Mapping[str, str]) -> pd.DataFrame:
    """``frame`` with each column cast to the version's recorded type (and in its order)."""
    missing = [c for c in columns if c not in frame.columns]
    if missing or len(columns) != len(frame.columns):
        raise DataFormatError(
            "columns differ from the ones recorded for this data version",
            expected=list(columns),
            found=[str(c) for c in frame.columns],
        )
    frame = frame[list(columns)]
    casts = {c: t for c, t in columns.items() if str(frame[c].dtype) != t}
    if not casts:
        return frame
    try:
        return frame.astype(casts)
    except (TypeError, ValueError) as exc:
        raise DataFormatError(
            f"a column no longer holds its recorded type: {exc}"[:500]
        ) from exc


def split_timestamps(
    frame: pd.DataFrame, column: str
) -> tuple[pd.DataFrame, list[datetime]]:
    """The payload columns and the UTC timestamps of ``column``, which must hold one per row."""
    if column not in frame.columns:
        raise DataFormatError(
            f"timestamp column {column!r} not in data", available_columns=list(frame.columns)
        )
    try:
        stamps = pd.to_datetime(frame[column], utc=True)
    except (ValueError, TypeError) as exc:
        raise DataFormatError(
            f"timestamp column {column!r} holds values that are not timestamps"
        ) from exc
    if stamps.isna().any():
        raise DataFormatError(f"timestamp column {column!r} has empty values")
    return frame.drop(columns=[column]), [ts.to_pydatetime() for ts in stamps]


def canonical_rows(frame: pd.DataFrame, observed_at: list[datetime]) -> list[dict]:
    """The rows exactly as the database stores them: JSON values, ``observed_at`` first."""
    # JSON round-trip turns numpy scalars into plain Python values (what the JSON column stores).
    rows = json.loads(frame.to_json(orient="records", double_precision=15))
    return [
        {"observed_at": ts.isoformat(), **{k: row[k] for k in sorted(row)}}
        for ts, row in zip(observed_at, rows, strict=True)
    ]


class RowHasher:
    """Incremental SHA-256 equal to ``sha256(json.dumps(all_rows, sort_keys=True))``."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256(b"[")
        self.rows = 0

    def add(self, row: dict) -> None:
        if self.rows:
            self._digest.update(b", ")
        self._digest.update(json.dumps(row, sort_keys=True).encode())
        self.rows += 1

    def add_frame(self, frame: pd.DataFrame, observed_at: list[datetime]) -> None:
        for row in canonical_rows(frame, observed_at):
            self.add(row)

    def hexdigest(self) -> str:
        final = self._digest.copy()
        final.update(b"]")
        return final.hexdigest()
