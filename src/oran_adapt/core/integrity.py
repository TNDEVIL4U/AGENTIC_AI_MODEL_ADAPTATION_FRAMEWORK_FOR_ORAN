"""SHA-256 checksums for model artifacts. A registered version's checksum is stored on it as the
``artifact.sha256`` tag and checked again before the version is evaluated, promoted or rolled
back to, so a corrupted or swapped artifact is refused instead of loaded."""

from __future__ import annotations

import hashlib
import os

from oran_adapt.core.errors import ArtifactError, ArtifactIntegrityError

_CHUNK = 1024 * 1024
# Largest model artifact (file, or directory in total) the framework downloads, loads or
# registers; Settings.artifact_max_bytes overrides it where settings are at hand.
DEFAULT_ARTIFACT_MAX_BYTES = 2 * 1024**3


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: str) -> str:
    """Checksum of a file, or of a directory's files (relative names and contents, in sorted
    order), so the same artifact downloaded twice gives the same value."""
    if os.path.isfile(path):
        return sha256_file(path)
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, path).replace(os.sep, "/").encode())
            digest.update(b"\0")
            digest.update(sha256_file(full).encode())
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


def verify_checksum(path: str, expected: str, **context: object) -> None:
    actual = sha256_path(path)
    if actual != expected:
        raise ArtifactIntegrityError(
            "artifact checksum mismatch; refusing to load it",
            path=path,
            expected=expected,
            actual=actual,
            **context,
        )
