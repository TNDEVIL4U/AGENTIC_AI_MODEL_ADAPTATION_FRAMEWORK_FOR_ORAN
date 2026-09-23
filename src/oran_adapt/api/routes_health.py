"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from oran_adapt import __version__
from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.schemas import ComponentHealth, HealthResponse, ReadyResponse
from oran_adapt.db.health import check_database

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness: the process is up. Does not touch dependencies."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyResponse)
@router.get("/readiness", response_model=ReadyResponse)
def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness: PostgreSQL and MLflow must both be reachable."""
    checks = {
        "database": lambda: check_database(request.app.state.engine),
        "mlflow": request.app.state.registry.ping,
    }
    components: list[ComponentHealth] = []
    for name, check in checks.items():
        try:
            check()
            components.append(ComponentHealth(name=name, ok=True))
        except AdaptationError as exc:
            components.append(ComponentHealth(name=name, ok=False, detail=exc.message))
    all_ok = all(c.ok for c in components)
    if not all_ok:
        response.status_code = 503
    return ReadyResponse(ready=all_ok, components=components)
