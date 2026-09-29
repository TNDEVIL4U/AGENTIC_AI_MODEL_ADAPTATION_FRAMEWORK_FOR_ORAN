"""The O-RAN-facing ingress: POST a drift notification, get back the job it produced (or a
reference to the job it already produced, if this event was already submitted).

The job is recorded QUEUED and run by a worker (``oran-adapt worker run``): poll
GET /adaptation/jobs/{id} for its outcome, or cancel it with POST /adaptation/jobs/{id}/cancel."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from oran_adapt.api.security import Submitter
from oran_adapt.core.enums import JobStatus
from oran_adapt.core.errors import JobNotFoundError
from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AdaptationEvent, AdaptationJob
from oran_adapt.orchestrator.jobs import (
    _to_response,
    list_jobs,
    request_cancel,
    submit_adaptation_job,
)

__all__ = ["JobNotFoundError", "router"]

router = APIRouter(prefix="/adaptation", tags=["adaptation"])


@router.post("/events", response_model=JobResponse)
def submit_event(
    event: DriftEvent,
    request: Request,
    response: Response,
    principal: Submitter,
) -> JobResponse:
    state = request.app.state
    result = submit_adaptation_job(
        state.session_factory,
        event,
        state.settings,
        registry=state.registry,
        llm_client=state.llm_client,
        workdir=state.settings.artifact_workdir,
        actor=principal.name,
        queue=state.job_queue,
    )
    response.status_code = 200 if result.duplicate else 201
    return result


@router.get("/jobs")
def get_jobs(
    request: Request,
    status: JobStatus | None = None,
    model_id: str | None = Query(default=None, max_length=200),
    tenant: str | None = Query(default=None, max_length=100),
    quarantined: bool | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Jobs, newest first (limit omitted: API_PAGINATION_DEFAULT_LIMIT)."""
    state = request.app.state
    with session_scope(state.session_factory) as session:
        return list_jobs(
            session,
            limit=limit or state.settings.api_pagination_default_limit,
            offset=offset,
            status=status,
            model_id=model_id,
            tenant=tenant,
            quarantined=quarantined,
        )


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: str, request: Request, response: Response, principal: Submitter
) -> JobResponse:
    """Cancel a job: 200 when it was queued and is now CANCELLED, 202 when it is running and
    its worker stops it at the next checkpoint (JOB_HEARTBEAT_S), 409 when it already ended or
    is registering or promoting."""
    state = request.app.state
    result, immediate = request_cancel(
        state.session_factory, state.settings, job_id, actor=principal.name
    )
    response.status_code = 200 if immediate else 202
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
