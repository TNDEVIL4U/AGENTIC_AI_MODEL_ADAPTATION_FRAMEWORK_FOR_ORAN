"""The O-RAN-facing ingress: POST a drift notification, get back the job it produced (or a
reference to the job it already produced, if this event was already submitted)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from oran_adapt.core.schemas import DriftEvent, JobResponse
from oran_adapt.orchestrator.jobs import submit_adaptation_job

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
