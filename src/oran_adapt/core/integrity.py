"""SHA-256 checksums for model artifacts. A registered version's checksum is stored on it as a
version tag (REGISTRY_TAGS_CHECKSUM) and checked again before the version is evaluated, promoted or rolled
back to, so a corrupted or swapped artifact is refused instead of loaded."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oran_adapt.core.errors import ArtifactError, ArtifactIntegrityError

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


@dataclass(frozen=True)
class ArtifactPolicy:
    """How model artifacts are bounded, hashed and tagged. A registry adapter carries one, so
    everything that downloads a version applies the same limits and reads the same tags."""

    max_bytes: int  # largest artifact (file, or directory in total) downloaded or loaded
    hash_chunk_bytes: int
    checksum_tag: str  # version tag holding the artifact's SHA-256
    status_tag: str  # version tag holding the ModelVersionStatus

    @classmethod
    def from_settings(cls, settings: Settings) -> ArtifactPolicy:
        return cls(
            max_bytes=settings.artifact_max_bytes,
            hash_chunk_bytes=settings.artifact_hash_chunk_bytes,
            checksum_tag=settings.registry_tags_checksum,
            status_tag=settings.registry_tags_status,
        )


def sha256_file(path: str, chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: str, chunk_bytes: int) -> str:
    """Checksum of a file, or of a directory's files (relative names and contents, in sorted
    order), so the same artifact downloaded twice gives the same value."""
    if os.path.isfile(path):
        return sha256_file(path, chunk_bytes)
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, path).replace(os.sep, "/").encode())
            digest.update(b"\0")
            digest.update(sha256_file(full, chunk_bytes).encode())
    return digest.hexdigest()


def path_size(path: str) -> int:
    """Bytes in a file, or in all files under a directory."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(
        os.path.getsize(os.path.join(root, name))
        for root, _, files in os.walk(path)
        for name in files
    )


def check_size(path: str, max_bytes: int, **context: object) -> int:
    """Refuse an artifact larger than ``max_bytes`` before anything loads it."""
    size = path_size(path)
    if size > max_bytes:
        raise ArtifactError(
            "model artifact exceeds the size limit; refusing to load it",
            path=path,
            size_bytes=size,
            max_bytes=max_bytes,
            **context,
        )
    return size


def verify_checksum(path: str, expected: str, *, chunk_bytes: int, **context: object) -> None:
    actual = sha256_path(path, chunk_bytes)
    if actual != expected:
        raise ArtifactIntegrityError(
            "artifact checksum mismatch; refusing to load it",
            path=path,
            expected=expected,
            actual=actual,
            **context,
        )
