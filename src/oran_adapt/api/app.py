"""FastAPI application factory. Dependencies are injected via app.state (no globals)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from oran_adapt import __version__
from oran_adapt.api.routes_adaptation import router as adaptation_router
from oran_adapt.api.routes_data import router as data_router
from oran_adapt.api.routes_health import router as health_router
from oran_adapt.api.routes_models import router as models_router
from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.logging import configure_logging
from oran_adapt.db.base import create_db_engine, make_session_factory
from oran_adapt.llm.client import build_llm_client
from oran_adapt.registry.client import MlflowRegistry


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

    @app.exception_handler(AdaptationError)
    async def _adaptation_error(_: Request, exc: AdaptationError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc), content=exc.to_dict())

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(adaptation_router, prefix="/api/v1")
    app.include_router(data_router, prefix="/api/v1")
    app.include_router(models_router, prefix="/api/v1")
    return app


def _status_for(exc: AdaptationError) -> int:
    return {
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
