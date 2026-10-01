"""Conformance suite for ``ArtifactStorePort`` adapters (artifact bytes by store-relative key).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_store(check, tmp_path):
        CHECKS[check](MyStore(...), Context(workdir=str(tmp_path), broken=...))

``workdir`` is a scratch directory for the local files the checks put and fetch; ``broken``
builds an instance of the same adapter whose store cannot be reached (None: cannot be
simulated). Rules: a stored file or directory reads back byte for byte; a key is written once
(artifacts are immutable); a missing key is ArtifactError; unsafe keys are refused; the adapter
pickles (worker processes); an unreachable store is RegistryUnavailableError.
"""

from __future__ import annotations

import functools
import itertools
import os
import pickle  # nosec B403 - the check pickles an adapter it was handed, never foreign data
from collections.abc import Callable
from dataclasses import dataclass, field

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import ArtifactError, RegistryUnavailableError
from oran_adapt.ports import ArtifactStorePort

_runs = itertools.count(1)
_UNSAFE_KEYS = ("", "/absolute", "../escape", "a/../../escape", "a//b", "a/./b", "a\\b")


@dataclass
class Context:
    workdir: str
    broken: Callable[[], ArtifactStorePort] | None = None
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def key(self, label: str) -> str:
        return f"conformance/{os.getpid()}-{self._run:04d}/{label}"

    def local(self, *parts: str) -> str:
        path = os.path.join(self.workdir, f"run{self._run:04d}", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path


def _write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _tree(root: str) -> dict[str, bytes]:
    out = {}
    for base, _, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = _read(full)
    return out


def _raises(action: Callable[[], object], error: type[Exception], what: str) -> None:
    try:
        action()
    except error:
        return
    except Exception as exc:  # any other class is the deviation reported
        raise ConformanceFailure(
            f"{what} must raise {error.__name__}, not {type(exc).__name__}: {exc}") from exc
    raise ConformanceFailure(f"{what} must raise {error.__name__}")


def check_protocol(port: ArtifactStorePort, ctx: Context) -> None:
    expect(isinstance(port, ArtifactStorePort), "does not implement ArtifactStorePort")


def check_file_roundtrip(port: ArtifactStorePort, ctx: Context) -> None:
    data = b"\x00model-bytes\xff" * 64
    key = ctx.key("single/model.bin")
    expect(not port.exists(key), "a key never written must not exist")
    uri = port.put(key, _write(ctx.local("src", "model.bin"), data))
    expect(isinstance(uri, str) and bool(uri), "put must return the stored artifact's URI")
    expect(port.exists(key), "a stored key must exist")
    got = port.get(key, ctx.local("dst"))
    expect(os.path.isfile(got), "get must return the path of the fetched file")
    expect(_read(got) == data, "a stored file must read back byte for byte")


def check_directory_roundtrip(port: ArtifactStorePort, ctx: Context) -> None:
    src = ctx.local("tree", "artifact")
    files = {"MLmodel": b"flavors: {}\n", "model.pkl": b"\x80\x05binary",
             "nested/deeper/weights.bin": bytes(range(256))}
    for rel, data in files.items():
        _write(os.path.join(src, *rel.split("/")), data)
    key = ctx.key("tree/artifact")
    port.put(key, src)
    got = port.get(key, ctx.local("fetched"))
    expect(os.path.isdir(got), "get of a stored directory must return a directory")
    expect(_tree(got) == files, "a stored directory must read back with the same layout")


def check_write_once(port: ArtifactStorePort, ctx: Context) -> None:
    key = ctx.key("once/model.bin")
    port.put(key, _write(ctx.local("once", "a.bin"), b"first"))
    _raises(lambda: port.put(key, _write(ctx.local("once", "b.bin"), b"second")),
            ArtifactError, "putting an existing key")
    expect(_read(port.get(key, ctx.local("once-dst"))) == b"first",
           "a refused second put must leave the first artifact unchanged")


def check_missing_key(port: ArtifactStorePort, ctx: Context) -> None:
    key = ctx.key("never/written")
    expect(port.exists(key) is False, "exists must be False for a key never written")
    _raises(lambda: port.get(key, ctx.local("missing")), ArtifactError, "get of a missing key")


def check_unsafe_keys_refused(port: ArtifactStorePort, ctx: Context) -> None:
    src = _write(ctx.local("unsafe", "x.bin"), b"x")
    for key in _UNSAFE_KEYS:
        _raises(functools.partial(port.put, key, src), ArtifactError, f"put with key {key!r}")
        _raises(functools.partial(port.get, key, ctx.local("unsafe-dst")), ArtifactError,
                f"get with key {key!r}")


def check_pickle(port: ArtifactStorePort, ctx: Context) -> None:
    key = ctx.key("pickled/model.bin")
    port.put(key, _write(ctx.local("pickled", "m.bin"), b"shared"))
    clone = pickle.loads(pickle.dumps(port))  # nosec B301 - our own object, just pickled
    expect(clone.exists(key), "a pickled copy must see the same store")
    expect(_read(clone.get(key, ctx.local("pickled-dst"))) == b"shared",
           "a pickled copy must read the same bytes")


def check_unreachable_store(port: ArtifactStorePort, ctx: Context) -> None:
    if ctx.broken is None:
        return
    broken = ctx.broken()
    src = _write(ctx.local("down", "m.bin"), b"x")
    _raises(lambda: broken.put(ctx.key("down/m.bin"), src), RegistryUnavailableError,
            "put to an unreachable store")


CHECKS: dict[str, Callable[[ArtifactStorePort, Context], None]] = {
    "protocol": check_protocol,
    "file_roundtrip": check_file_roundtrip,
    "directory_roundtrip": check_directory_roundtrip,
    "write_once": check_write_once,
    "missing_key": check_missing_key,
    "unsafe_keys_refused": check_unsafe_keys_refused,
    "pickle": check_pickle,
    "unreachable_store": check_unreachable_store,
}


def run(port: ArtifactStorePort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
