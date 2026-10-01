"""Putting models into the registry and describing what is there, over the ports only.

``publish_model`` serializes a native model with the model handler and stores the directory as
a new registry version; the registry never sees the model object. ``record_artifact_checksum``
fixes the SHA-256 of what the registry actually stores, so every later load can be checked.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import ArtifactError
from oran_adapt.core.integrity import check_size, sha256_path
from oran_adapt.core.model_uri import ModelUri, check_model_name

if TYPE_CHECKING:
    import pandas as pd

    from oran_adapt.ports import ModelHandlerPort, ModelRegistryPort


def publish_model(
    registry: ModelRegistryPort,
    handler: ModelHandlerPort,
    name: str,
    model: object,
    *,
    framework: str,
    workdir: str | None,
    metrics: dict[str, float] | None = None,
    tags: dict[str, str] | None = None,
    input_frame: pd.DataFrame | None = None,
    input_name: str | None = None,
    input_digest: str | None = None,
) -> str:
    """Serialize ``model`` and register it as the next version of ``name``; returns the
    version. ``workdir`` holds the temporary artifact directory (None: the system temp
    directory). The size limit of ``registry.artifact_policy`` applies before upload."""
    check_model_name(name)
    if workdir is not None:
        os.makedirs(workdir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publish-", dir=workdir) as tmp:
        artifact_dir = handler.save(model, framework, os.path.join(tmp, "model"))
        check_size(artifact_dir, registry.artifact_policy.max_bytes, model=name)
        return registry.create_version(
            name,
            artifact_dir,
            metrics=metrics,
            tags=tags,
            input_frame=input_frame,
            input_name=input_name,
            input_digest=input_digest,
        )


def register_candidate(
    registry: ModelRegistryPort,
    handler: ModelHandlerPort,
    name: str,
    artifact_path: str,
    *,
    framework: str,
    workdir: str,
    metrics: dict[str, float],
    tags: dict[str, str] | None = None,
) -> str:
    """Register a candidate produced by the adaptation engines (a local joblib file whose
    checksum the caller has verified) as the next version of ``name``."""
    import joblib

    try:
        model = joblib.load(artifact_path)
    except Exception as exc:
        raise ArtifactError(
            f"could not load candidate artifact: {artifact_path}", cause=str(exc)
        ) from exc
    return publish_model(
        registry, handler, name, model,
        framework=framework, workdir=workdir, metrics=metrics, tags=tags,
    )


def record_artifact_checksum(
    registry: ModelRegistryPort, name: str, version: str, workdir: str
) -> str:
    """Download ``version``'s artifacts, hash them and store the hash as the checksum version
    tag; returns the hash. Called right after registration, so the checksum is of what the
    registry stores rather than of the local copy."""
    policy = registry.artifact_policy
    local = registry.download_artifacts(name, version, os.path.join(workdir, f"sha-{version}"))
    digest = sha256_path(local, policy.hash_chunk_bytes)
    registry.set_version_tags(name, version, {policy.checksum_tag: digest})
    return digest


def describe_versions(registry: ModelRegistryPort, name: str) -> list[dict[str, Any]]:
    """Every registered version of ``name`` with its URI, aliases, tags and source - the
    registry half of a model's lineage (the data half lives in the database)."""
    versions = registry.list_versions(name)
    by_version: dict[str, list[str]] = {}
    for alias, version in registry.get_registered_model(name).aliases.items():
        by_version.setdefault(str(version), []).append(alias)
    return [
        {
            "version": v.version,
            "uri": str(ModelUri.for_version(name, v.version)),
            "aliases": sorted(by_version.get(v.version, [])),
            "tags": dict(v.tags),
            "run_id": v.source,
            "status": v.status,
            "created_at_ms": v.created_at_ms,
        }
        for v in versions
    ]
