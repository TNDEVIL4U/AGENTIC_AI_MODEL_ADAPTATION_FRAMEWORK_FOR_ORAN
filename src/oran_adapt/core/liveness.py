"""The worker's liveness file (WORKER_HEALTH_FILE).

A worker has no HTTP server, so its liveness is a file: the worker touches it on every turn of
its loop and on every checkpoint of a running job, and ``oran-adapt worker health`` (the image
HEALTHCHECK and the Kubernetes exec probe) fails when the file is missing or older than
WORKER_HEALTH_MAX_AGE_S. A worker stuck anywhere, idle or busy, stops touching it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from oran_adapt.core.errors import ConfigurationError, WorkerUnhealthyError

logger = logging.getLogger(__name__)


def beat(path: str | None) -> None:
    """Touch the liveness file. Never fails the worker: a write error is logged, and the probe
    then reports the worker unhealthy, which is the truth."""
    if not path:
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    except OSError as exc:
        logger.warning("could not touch the liveness file %s: %s", path, type(exc).__name__)


def check(path: str | None, max_age_s: float, *, now: float | None = None) -> dict[str, object]:
    """The probe: the file's age, or WorkerUnhealthyError."""
    if not path:
        raise ConfigurationError("WORKER_HEALTH_FILE is not set, so there is nothing to check",
                                 key="WORKER_HEALTH_FILE")
    try:
        modified = Path(path).stat().st_mtime
    except OSError as exc:
        raise WorkerUnhealthyError("the worker has not written its liveness file",
                                   path=path, cause=type(exc).__name__) from exc
    age = (time.time() if now is None else now) - modified
    if age > max_age_s:
        raise WorkerUnhealthyError("the worker's loop has stopped turning", path=path,
                                   age_s=round(age, 1), max_age_s=max_age_s)
    return {"healthy": True, "age_s": round(max(age, 0.0), 1), "max_age_s": max_age_s}
