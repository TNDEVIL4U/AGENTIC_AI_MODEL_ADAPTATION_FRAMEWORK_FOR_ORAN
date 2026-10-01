"""Liveness, readiness and Prometheus metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from oran_adapt import __version__
from oran_adapt.api.security import READ, public, require
from oran_adapt.core import metrics
from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.schemas import ComponentHealth, HealthResponse, ReadyResponse
from oran_adapt.db.health import check_database

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(public)])
def health() -> HealthResponse:
    """Liveness: the process is up. Does not touch dependencies."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyResponse, dependencies=[Depends(public)])
@router.get("/readiness", response_model=ReadyResponse, dependencies=[Depends(public)])
def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness: the database, the model registry and the serving system must be reachable."""
    checks = {
        "database": lambda: check_database(request.app.state.engine),
        "mlflow": request.app.state.registry.ping,
        "deployment": request.app.state.deployer.port.ping,
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


@router.get(
    "/metrics",
    include_in_schema=False,
    dependencies=[Depends(require(READ, unless="metrics_public"))],
)
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint. Needs a key (any role) unless ``metrics_public`` is set."""
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
