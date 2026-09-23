"""Per-model job lock: at most one adaptation job runs for a model at a time, so two drift
events can never both move its LIVE alias.

The lock is a ModelLock row keyed by model_id. It is taken in the same transaction that
inserts the job, so a job is only ever persisted together with its lock: whoever loses the race
gets an IntegrityError on the primary key and nothing of theirs is recorded. A lock past its
``expires_at`` is considered abandoned (the process holding it died) and is taken over with a
conditional UPDATE, which again only one caller can win.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from oran_adapt.db.models import ModelLock


def _now() -> datetime:
    return datetime.now(UTC)


def take_over_expired_lock(session: Session, model_id: str, job_id: str, ttl_s: float) -> bool:
    """Hand an expired lock to ``job_id``. True if it was expired and is now ours."""
    now = _now()
    result = session.execute(
        update(ModelLock)
        .where(ModelLock.model_id == model_id, ModelLock.expires_at < now)
        .values(job_id=job_id, acquired_at=now, expires_at=now + timedelta(seconds=ttl_s))
    )
    return result.rowcount == 1


def add_lock(session: Session, model_id: str, job_id: str, ttl_s: float) -> None:
    """Stage a new lock row; the caller's flush raises IntegrityError if one exists."""
    now = _now()
    session.add(
        ModelLock(
            model_id=model_id,
            job_id=job_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
    )


def release_lock(session: Session, model_id: str, job_id: str) -> None:
    """Release only if ``job_id`` still holds it (a taken-over lock belongs to someone else)."""
    session.execute(
        delete(ModelLock).where(ModelLock.model_id == model_id, ModelLock.job_id == job_id)
    )
