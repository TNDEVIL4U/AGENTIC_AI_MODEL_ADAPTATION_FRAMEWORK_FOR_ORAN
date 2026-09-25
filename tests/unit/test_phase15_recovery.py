"""Rule 22 scenario 21 (service restart): a job whose process died mid-run - here, one left in
ADAPTING by a crash or restart - does not block its model forever and does not stay in a running
state. After the restart the API serves again, the next event takes over the expired lock and
runs, and the orphan is recorded FAILED with JOB_ABANDONED; a worker that somehow outlived the
crash can no longer move it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_phase14_member1 import _path, _seed_three_similar, registry, session_factory

from oran_adapt.core.enums import JobStatus
from oran_adapt.core.errors import InvalidTransitionError
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AdaptationJob, ModelLock
from oran_adapt.orchestrator.jobs import _transition

__all__ = ["registry", "session_factory"]  # fixtures re-used from test_phase14_member1


def _orphan(session_factory, *, model_id: str, job_id: str, status: JobStatus) -> None:
    """What a crash leaves behind: a job mid-run and its lock, now past its expiry."""
    past = datetime.now(UTC) - timedelta(hours=2)
    with session_scope(session_factory) as session:
        session.add(
            AdaptationJob(
                job_id=job_id,
                idempotency_key=f"key-{job_id}",
                model_id=model_id,
                status=status,
                event={"model_id": model_id},
            )
        )
        session.add(
            ModelLock(
                model_id=model_id,
                job_id=job_id,
                acquired_at=past,
                expires_at=past + timedelta(hours=1),
            )
        )


def test_after_a_restart_the_orphaned_job_is_failed_and_the_model_runs_again(
    session_factory, registry, migrated_settings, client
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-r", name="cell_r"
    )
    _orphan(session_factory, model_id="cell-r", job_id="crashed-job", status=JobStatus.PROMOTING)

    # `client` is a freshly started app on the same database: the restarted service.
    assert client.get("/api/v1/health").status_code == 200
    r = client.post(
        "/api/v1/adaptation/events",
        json={"model_id": "cell-r", "event_id": "evt-after-restart", "drift_detected": False},
    )
    assert r.status_code in (200, 201), r.text
    new_job = r.json()
    assert new_job["status"] == JobStatus.COMPLETED, new_job

    with session_scope(session_factory) as session:
        orphan = session.query(AdaptationJob).filter_by(job_id="crashed-job").one()
        assert orphan.status == JobStatus.FAILED
        assert orphan.error["code"] == "JOB_ABANDONED"
        assert orphan.error["context"]["last_stage"] == JobStatus.PROMOTING
        assert orphan.error["context"]["taken_over_by"] == new_job["job_id"]
        # It died while moving LIVE, so an operator is told to check the alias.
        assert orphan.error["context"]["needs_reconciliation"] is True
        assert session.get(ModelLock, "cell-r") is None  # the new job released it
    assert _path(session_factory, "crashed-job") == [JobStatus.FAILED]

    # A worker of the crashed job that is somehow still alive cannot move it any further.
    with pytest.raises(InvalidTransitionError):
        _transition(session_factory, "crashed-job", to_status=JobStatus.COMPLETED)

    job = client.get("/api/v1/adaptation/jobs/crashed-job")
    assert job.status_code == 200 and job.json()["status"] == JobStatus.FAILED
