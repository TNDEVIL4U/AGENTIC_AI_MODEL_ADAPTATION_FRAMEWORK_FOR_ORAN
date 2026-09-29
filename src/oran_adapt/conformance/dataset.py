"""Conformance suite for ``DatasetPort`` adapters (data read by reference).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
context writes objects into the store the adapter reads (a directory, a bucket, a test
server) and names URIs the adapter must refuse or cannot find::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_store(check):
        CHECKS[check](MyDataset(...), Context(put=..., missing=..., outside=...))

``PINNED_CHECKS`` apply to adapters with the ``pinned`` feature (versioned object stores): a
URI returned as ``pinned_uri`` keeps reading the same bytes after the key is overwritten.
docs/adapters/dataset.md explains each rule.
"""

from __future__ import annotations

import io
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import DatasetNotFoundError, DataSourceNotAllowedError
from oran_adapt.datastore.access import DataAccess
from oran_adapt.datastore.formats import split_timestamps
from oran_adapt.datastore.versioning import content_hash
from oran_adapt.ports import DatasetPort

_runs = itertools.count(1)

# Integers in the first rows and a float later: a text format infers different column types
# per batch, and the rows must still hash the same whatever the batch size.
_CSV = (
    b"ts,cell,prb_util,users\n"
    b"2026-09-01T00:00:00+00:00,c1,1,10\n"
    b"2026-09-01T00:01:00+00:00,c2,2,11\n"
    b"2026-09-01T00:02:00+00:00,c1,2.5,12\n"
    b"2026-09-01T00:03:00+00:00,c3,4,13\n"
    b"2026-09-01T00:04:00+00:00,c2,5.25,14\n"
)
_JSONL = b"".join(
    b'{"ts": "2026-09-01T00:0%d:00+00:00", "cell": "c%d", "prb_util": %s, "users": %d}\n'
    % (i, i % 3 + 1, v, 10 + i)
    for i, v in enumerate([b"1", b"2", b"2.5", b"4", b"5.25"])
)


@dataclass
class Context:
    put: Callable[[str, bytes], str]
    """``put(name, data)`` stores ``data`` as object ``name`` and returns its URI; putting the
    same name again replaces the object."""
    missing: Callable[[str], str]
    """``missing(name)``: a URI the adapter accepts but no object exists at."""
    outside: str
    """A URI of the adapter's scheme that its allow-list does not cover."""
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def name(self, label: str, suffix: str = ".csv") -> str:
        return f"conformance-{self._run}-{label}{suffix}"


def _read(port: DatasetPort, uri: str) -> bytes:
    with port.open(uri) as fh:
        return fh.read()


def check_protocol(port: DatasetPort, ctx: Context) -> None:
    expect(isinstance(port, DatasetPort), "does not implement DatasetPort")
    expect(bool(port.schemes) and all(s == s.lower() for s in port.schemes),
           "schemes must be a non-empty set of lower-case URI schemes")


def check_stat(port: DatasetPort, ctx: Context) -> None:
    uri = ctx.put(ctx.name("stat"), _CSV)
    port.check(uri)
    first, second = port.stat(uri), port.stat(uri)
    expect(first.uri == uri, "stat did not echo the URI it was asked about")
    expect(bool(first.pinned_uri), "stat returned no pinned_uri")
    expect(first.fingerprint == second.fingerprint,
           "two stats of an unchanged object gave different fingerprints")
    expect(first.pinned_uri == second.pinned_uri,
           "two stats of an unchanged object gave different pinned URIs")
    if first.size_bytes is not None:
        expect(first.size_bytes == len(_CSV),
               f"size_bytes is {first.size_bytes}, the object has {len(_CSV)} bytes")


def check_open_reads_bytes(port: DatasetPort, ctx: Context) -> None:
    uri = ctx.put(ctx.name("open"), _CSV)
    stat = port.stat(uri)
    expect(_read(port, uri) == _CSV, "open returned other bytes than were stored")
    expect(_read(port, stat.pinned_uri) == _CSV,
           "opening the pinned URI returned other bytes than were stored")
    with port.open(uri) as fh:
        expect(fh.seekable(), "open must return a seekable file (Parquet reads its footer)")


def check_change_detected(port: DatasetPort, ctx: Context) -> None:
    """A replaced object must be told apart from the registered one: by its fingerprint, or
    (for a pinned store) by a new pinned URI."""
    name = ctx.name("change")
    before = port.stat(ctx.put(name, _CSV))
    after = port.stat(ctx.put(name, _CSV.replace(b"c3,4,13", b"c3,49,13")))
    expect(before.fingerprint != after.fingerprint or before.pinned_uri != after.pinned_uri,
           "overwriting the object changed neither its fingerprint nor its pinned URI")
    expect(before.fingerprint is not None or before.pinned_uri != after.pinned_uri,
           "the adapter gives no fingerprint and no version pin, so a change is invisible")


def check_missing(port: DatasetPort, ctx: Context) -> None:
    uri = ctx.missing(ctx.name("missing"))
    port.check(uri)
    for action in ("stat", "open"):
        try:
            getattr(port, action)(uri)
        except DatasetNotFoundError:
            continue
        except Exception as exc:
            raise ConformanceFailure(
                f"{action} of a missing object raised {type(exc).__name__}, "
                "not DatasetNotFoundError"
            ) from exc
        raise ConformanceFailure(f"{action} of a missing object did not raise")


def check_refuses_outside(port: DatasetPort, ctx: Context) -> None:
    for action in ("check", "stat", "open"):
        try:
            getattr(port, action)(ctx.outside)
        except DataSourceNotAllowedError:
            continue
        except Exception as exc:
            raise ConformanceFailure(
                f"{action} outside the allow-list raised {type(exc).__name__}, "
                "not DataSourceNotAllowedError"
            ) from exc
        raise ConformanceFailure(f"{action} accepted a URI outside the allow-list")


def _inline_hash(data: bytes, fmt: str) -> str:
    whole = (pd.read_csv(io.BytesIO(data)) if fmt == "csv"
             else pd.read_json(io.BytesIO(data), lines=True))
    payload, stamps = split_timestamps(whole, "ts")
    return content_hash(payload, stamps)


def check_rows_stable(port: DatasetPort, ctx: Context) -> None:
    """Read through DataAccess, the rows hash the same at any batch size, and the same as
    those rows sent inline."""
    for fmt, data in (("csv", _CSV), ("jsonl", _JSONL)):
        uri = ctx.put(ctx.name("rows", f".{fmt}"), data)
        expected = _inline_hash(data, fmt)
        for batch in (1, 2, 1000):
            scan = DataAccess([port], chunk_rows=batch).scan(uri, timestamp_column="ts")
            expect(scan.row_count == 5, f"{fmt} at batch {batch}: {scan.row_count} rows, not 5")
            expect(scan.content_hash == expected,
                   f"{fmt} at batch {batch}: the rows hash differently from the same rows "
                   "sent inline")


def check_pinned_stable(port: DatasetPort, ctx: Context) -> None:
    name = ctx.name("pinned")
    pinned = port.stat(ctx.put(name, _CSV)).pinned_uri
    ctx.put(name, _CSV.replace(b"c3,4,13", b"c3,49,13"))
    expect(_read(port, pinned) == _CSV,
           "after the key was overwritten, its pinned URI read the new bytes")


CHECKS: dict[str, Callable[[DatasetPort, Context], None]] = {
    "protocol": check_protocol,
    "stat": check_stat,
    "open_reads_bytes": check_open_reads_bytes,
    "change_detected": check_change_detected,
    "missing": check_missing,
    "refuses_outside": check_refuses_outside,
    "rows_stable": check_rows_stable,
}

PINNED_CHECKS: dict[str, Callable[[DatasetPort, Context], None]] = {
    "pinned_stable": check_pinned_stable,
}


def run(port: DatasetPort, ctx: Context, *, pinned: bool = False) -> list[str]:
    """Run every check (and the pinned checks when ``pinned``) in order; returns their names.
    Stops at the first failure."""
    checks = dict(CHECKS)
    if pinned:
        checks.update(PINNED_CHECKS)
    for check in checks.values():
        check(port, ctx)
    return list(checks)


__all__ = ["CHECKS", "PINNED_CHECKS", "ConformanceFailure", "Context", "run"]
