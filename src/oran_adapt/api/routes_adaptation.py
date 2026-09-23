"""The O-RAN-facing ingress: POST a drift notification, get back the job it produced (or a
reference to the job it already produced, if this event was already submitted)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AdaptationEvent, AdaptationJob
from oran_adapt.orchestrator.jobs import _to_response, submit_adaptation_job


class JobNotFoundError(AdaptationError):
    code = "JOB_NOT_FOUND"

router = APIRouter(prefix="/adaptation", tags=["adaptation"])


@router.post("/events", response_model=JobResponse)
def submit_event(event: DriftEvent, request: Request, response: Response) -> JobResponse:
    state = request.app.state
    result = submit_adaptation_job(
        state.session_factory,
        event,
        state.settings,
        registry=state.registry,
        llm_client=state.llm_client,
        workdir=state.settings.artifact_workdir,
    )
    response.status_code = 200 if result.duplicate else 201
    return result


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    """A job's current status, result or error, and every state transition it went through."""
    with session_scope(request.app.state.session_factory) as session:
        job = session.execute(
            select(AdaptationJob).where(AdaptationJob.job_id == job_id)
        ).scalar_one_or_none()
        if job is None:
            raise JobNotFoundError(f"job '{job_id}' not found", job_id=job_id)
        events = (
            session.execute(
                select(AdaptationEvent)
                .where(AdaptationEvent.job_id == job_id)
                .order_by(AdaptationEvent.id)
            )
            .scalars()
            .all()
        )
        body = _to_response(job, duplicate=False).model_dump(mode="json")
        body["transitions"] = [
            {
                "from_status": e.from_status,
                "to_status": e.to_status,
                "message": e.message,
                "at": e.created_at.isoformat(),
            }
            for e in events
        ]
        return body
