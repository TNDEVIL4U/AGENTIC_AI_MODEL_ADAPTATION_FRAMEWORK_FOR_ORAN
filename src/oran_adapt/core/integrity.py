"""SHA-256 checksums for model artifacts. A registered version's checksum is stored on it as the
``artifact.sha256`` tag and checked again before the version is evaluated, promoted or rolled
back to, so a corrupted or swapped artifact is refused instead of loaded."""

from __future__ import annotations

import hashlib
import os

from oran_adapt.core.errors import ArtifactIntegrityError

_CHUNK = 1024 * 1024


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
