"""FastAPI application factory. Dependencies are injected via app.state (no globals)."""

from __future__ import annotations

import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oran_adapt import __version__
from oran_adapt.api.routes_adaptation import router as adaptation_router
from oran_adapt.api.routes_data import current_router as current_data_router
from oran_adapt.api.routes_data import router as data_router
from oran_adapt.api.routes_health import router as health_router
from oran_adapt.api.routes_models import router as models_router
from oran_adapt.api.security import READ_ROLES, require_roles
from oran_adapt.core import correlation, metrics
from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.logging import configure_logging
from oran_adapt.db.base import create_db_engine, make_session_factory
from oran_adapt.llm.client import build_llm_client
from oran_adapt.registry.client import MlflowRegistry


class RequestContextMiddleware:
    """Gives every request a correlation id (the caller's ``X-Correlation-ID`` when it is a safe
    token, otherwise a new one), echoes it in the response, and records HTTP metrics labelled by
    route template (never the raw path, so ids do not multiply series)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = dict(scope.get("headers") or []).get(correlation.HEADER.lower().encode())
        cid = correlation.sanitize(sent.decode("latin-1") if sent else None)
        cid = cid or correlation.new_correlation_id()
        token = correlation.set_correlation_id(cid)
        status = {"code": 500}
        started = time.perf_counter()

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                MutableHeaders(scope=message).append(correlation.HEADER, cid)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method", "")
            metrics.HTTP_REQUESTS.labels(method, route, str(status["code"])).inc()
            metrics.HTTP_DURATION.labels(method, route).observe(time.perf_counter() - started)
            correlation.reset_correlation_id(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(title="O-RAN Model Adaptation Framework", version=__version__)
    app.state.settings = settings
    app.state.engine = create_db_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.registry = MlflowRegistry(
        settings.mlflow_tracking_uri,
        settings.mlflow_registry_uri,
        skops_trusted_types=settings.mlflow_skops_trusted_types,
    )
    app.state.llm_client = build_llm_client(settings)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AdaptationError)
    async def _adaptation_error(_: Request, exc: AdaptationError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content=exc.to_dict())

    # Any role may read; write endpoints add their own stricter role check.
    authenticated = [Depends(require_roles(*READ_ROLES))]
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(adaptation_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(data_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(current_data_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(models_router, prefix="/api/v1", dependencies=authenticated)
    return app


def _status_for(exc: AdaptationError) -> int:
    return {
        "UNAUTHENTICATED": 401,
        "FORBIDDEN": 403,
        "MODEL_NOT_FOUND": 404,
        "JOB_NOT_FOUND": 404,
        "DATASET_NOT_FOUND": 404,
        "CONFLICT": 409,
        "DATA_VERSION_CONFLICT": 409,
        "MODEL_BUSY": 409,
        "INVALID_STATE_TRANSITION": 409,
        "ARTIFACT_INTEGRITY_FAILED": 422,
        "MLFLOW_UNAVAILABLE": 503,
        "DATABASE_UNAVAILABLE": 503,
        "JOB_TIMEOUT": 504,
    }.get(exc.code, 500)
