"""Conformance suite for ``ModelRegistryPort`` adapters.

Each check takes a fresh-or-shared registry and a ``Context`` (a scratch directory and a model
name prefix, so checks never collide) and raises ConformanceFailure on a deviation. Run every
check with ``run(registry, context)`` or parametrize a test over ``CHECKS``::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_adapter(check, tmp_path):
        CHECKS[check](MyRegistry(...), Context(tmp_path))

Model names are lower-case with hyphens and aliases are plain words, the subset every
backend's naming rules accept; artifacts are opaque directories (the registry never loads a
model). docs/adapters/registry.md explains each rule.
"""

from __future__ import annotations

import os
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import ModelNotFoundError
from oran_adapt.core.integrity import ArtifactPolicy, sha256_path
from oran_adapt.core.model_uri import ModelUri
from oran_adapt.ports import ModelRegistryPort
from oran_adapt.ports.registry import READY


@dataclass
class Context:
    workdir: Path
    prefix: str = "conformance"
    _count: int = field(default=0, repr=False)

    def name(self, label: str) -> str:
        return f"{self.prefix}-{label}"

    def artifact(self, label: str) -> str:
        """A small artifact directory: a text file, a binary file and a nested file."""
        self._count += 1
        root = self.workdir / f"artifact-{label}-{self._count}"
        (root / "sub").mkdir(parents=True)
        (root / "model.bin").write_bytes(bytes(range(256)) * 4 + label.encode())
        (root / "meta.json").write_text(f'{{"label": "{label}"}}', encoding="utf-8")
        (root / "sub" / "weights.dat").write_bytes(os.urandom(512))
        return str(root)

    def scratch(self, label: str) -> str:
        self._count += 1
        path = self.workdir / f"scratch-{label}-{self._count}"
        path.mkdir(parents=True)
        return str(path)


def _raises_not_found(action: Callable[[], object], what: str) -> None:
    try:
        action()
    except ModelNotFoundError:
        return
    except Exception as exc:
        raise ConformanceFailure(
            f"{what}: expected ModelNotFoundError, got {type(exc).__name__}: {exc}"
        ) from exc
    raise ConformanceFailure(f"{what}: expected ModelNotFoundError, nothing was raised")


def check_protocol(registry: ModelRegistryPort, ctx: Context) -> None:
    expect(isinstance(registry, ModelRegistryPort), "does not implement ModelRegistryPort")
    expect(
        isinstance(registry.artifact_policy, ArtifactPolicy),
        "artifact_policy is not an ArtifactPolicy",
    )


def check_ping(registry: ModelRegistryPort, ctx: Context) -> None:
    registry.ping()


def check_missing_model(registry: ModelRegistryPort, ctx: Context) -> None:
    name = ctx.name("absent")
    _raises_not_found(lambda: registry.get_registered_model(name), "get_registered_model")
    _raises_not_found(lambda: registry.list_versions(name), "list_versions")
    _raises_not_found(lambda: registry.get_version(name, "1"), "get_version")
    _raises_not_found(lambda: registry.get_version_metrics(name, "1"), "get_version_metrics")
    _raises_not_found(lambda: registry.get_version_by_alias(name, "live"), "get_version_by_alias")
    _raises_not_found(lambda: registry.set_alias(name, "live", "1"), "set_alias")
    _raises_not_found(
        lambda: registry.download_artifacts(name, "1", ctx.scratch("absent")), "download"
    )


def check_versions(registry: ModelRegistryPort, ctx: Context) -> None:
    """Numbering per model from "1", oldest first, tags, status and metrics as created."""
    first, second = ctx.name("versions-a"), ctx.name("versions-b")
    v1 = registry.create_version(
        first, ctx.artifact("a1"), metrics={"rmse": 0.5, "r2": 0.9}, tags={"stage": "one"}
    )
    v2 = registry.create_version(first, ctx.artifact("a2"))
    b1 = registry.create_version(second, ctx.artifact("b1"), tags={"stage": "b"})
    expect((v1, v2, b1) == ("1", "2", "1"), f"versions not numbered per model: {v1, v2, b1}")

    listed = registry.list_versions(first)
    expect([v.version for v in listed] == ["1", "2"], "list_versions is not oldest first")
    expect(all(v.name == first for v in listed), "list_versions names the wrong model")
    got = registry.get_version(first, "1")
    expect(got.status == READY, f"a created version is {got.status}, not {READY}")
    expect(dict(got.tags).get("stage") == "one", f"tags not stored: {dict(got.tags)}")
    expect(
        got.created_at_ms is None or got.created_at_ms > 0, "created_at_ms is not a timestamp"
    )
    metrics = registry.get_version_metrics(first, "1")
    expect(metrics == {"rmse": 0.5, "r2": 0.9}, f"metrics not stored: {metrics}")
    expect(registry.get_version_metrics(first, "2") == {}, "metrics invented for version 2")
    _raises_not_found(lambda: registry.get_version(first, "3"), "get_version of version 3")
    expect(registry.get_registered_model(first).name == first, "registered model name differs")


def check_download_roundtrip(registry: ModelRegistryPort, ctx: Context) -> None:
    name = ctx.name("download")
    source = ctx.artifact("download")
    version = registry.create_version(name, source)
    local = registry.download_artifacts(name, version, ctx.scratch("download"))
    expect(os.path.isdir(local), "download_artifacts did not return a directory")
    chunk = registry.artifact_policy.hash_chunk_bytes
    expect(
        sha256_path(local, chunk) == sha256_path(source, chunk),
        "downloaded artifact differs from what was stored",
    )


def check_tag_merge(registry: ModelRegistryPort, ctx: Context) -> None:
    name = ctx.name("tags")
    version = registry.create_version(name, ctx.artifact("tags"), tags={"a": "1", "b": "2"})
    registry.set_version_tags(name, version, {"b": "3", "c": "4"})
    tags = dict(registry.get_version(name, version).tags)
    expect(
        {k: tags.get(k) for k in "abc"} == {"a": "1", "b": "3", "c": "4"},
        f"set_version_tags does not merge: {tags}",
    )
    _raises_not_found(
        lambda: registry.set_version_tags(name, "99", {"x": "y"}), "set_version_tags on v99"
    )


def check_aliases(registry: ModelRegistryPort, ctx: Context) -> None:
    name = ctx.name("aliases")
    v1 = registry.create_version(name, ctx.artifact("alias1"))
    v2 = registry.create_version(name, ctx.artifact("alias2"))
    expect(registry.get_registered_model(name).aliases == {}, "a new model has aliases")
    _raises_not_found(lambda: registry.get_version_by_alias(name, "live"), "unset alias")
    _raises_not_found(lambda: registry.set_alias(name, "live", "99"), "set_alias to v99")

    registry.set_alias(name, "live", v1)
    expect(registry.get_version_by_alias(name, "live") == v1, "alias not set")
    registry.set_alias(name, "live", v2)
    registry.set_alias(name, "champion", v1)
    expect(registry.get_version_by_alias(name, "live") == v2, "alias did not move")
    aliases = dict(registry.get_registered_model(name).aliases)
    expect(aliases == {"live": v2, "champion": v1}, f"registered aliases wrong: {aliases}")

    expect(ModelUri.parse(f"model://{name}@live").resolve(registry).version == v2, "URI@alias")
    expect(ModelUri.for_version(name, v1).resolve(registry).version == v1, "URI/version")

    registry.delete_alias(name, "live")
    _raises_not_found(lambda: registry.get_version_by_alias(name, "live"), "deleted alias")
    registry.delete_alias(name, "live")  # deleting an unset alias is a no-op
    expect(
        dict(registry.get_registered_model(name).aliases) == {"champion": v1},
        "delete_alias removed the wrong alias",
    )


def check_pickle(registry: ModelRegistryPort, ctx: Context) -> None:
    """A job worker process receives the registry pickled; the copy reads the same data."""
    name = ctx.name("pickle")
    version = registry.create_version(name, ctx.artifact("pickle"), tags={"k": "v"})
    copy: ModelRegistryPort = pickle.loads(pickle.dumps(registry))
    expect(dict(copy.get_version(name, version).tags).get("k") == "v", "pickled copy differs")


CHECKS: dict[str, Callable[[ModelRegistryPort, Context], None]] = {
    "protocol": check_protocol,
    "ping": check_ping,
    "missing_model": check_missing_model,
    "versions": check_versions,
    "download_roundtrip": check_download_roundtrip,
    "tag_merge": check_tag_merge,
    "aliases": check_aliases,
    "pickle": check_pickle,
}


def run(registry: ModelRegistryPort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(registry, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
