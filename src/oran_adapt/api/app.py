"""FastAPI application factory. Dependencies are injected via app.state (no globals)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oran_adapt import __version__
from oran_adapt.api.ratelimit import TokenBuckets
from oran_adapt.api.routes_adaptation import router as adaptation_router
from oran_adapt.api.routes_config import router as config_router
from oran_adapt.api.routes_data import current_router as current_data_router
from oran_adapt.api.routes_data import router as data_router
from oran_adapt.api.routes_health import router as health_router
from oran_adapt.api.routes_models import router as models_router
from oran_adapt.api.routes_notifications import router as notifications_router
from oran_adapt.api.routes_rollouts import router as rollouts_router
from oran_adapt.api.security import DOCS_PATHS, READ, enforce_route_policies, require
from oran_adapt.bootstrap import (
    build_auth,
    build_data_access,
    build_deployer,
    build_job_queue,
    build_llm,
    build_model_handler,
    build_notifiers,
    build_policy,
    build_registry,
    build_rollout_metrics,
    configure_tracing,
)
from oran_adapt.core import correlation, metrics
from oran_adapt.core.config import Settings, get_settings
from oran_adapt.core.errors import AdaptationError
from oran_adapt.core.event_mapping import load_mappers
from oran_adapt.core.logging import configure_logging
from oran_adapt.db.base import create_db_engine, make_session_factory
from oran_adapt.notifications.dispatcher import Dispatcher, DispatcherThread

logger = logging.getLogger(__name__)


class BodySizeLimitMiddleware:
    """Refuses a request body larger than ``max_bytes`` with a structured 413, whether the
    client declares its size (Content-Length) or streams it chunked, so an oversized upload is
    never buffered in full."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._refuse(scope, receive, send)
            return
        seen = {"bytes": 0, "too_large": False, "refused": False}

        async def _receive() -> Message:
            if seen["too_large"]:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                seen["bytes"] += len(message.get("body", b""))
                if seen["bytes"] > self.max_bytes:
                    # Stop reading here. Raising would be turned into a 400 by the framework's
                    # body parser, so the app is handed an empty end of body instead and
                    # whatever it answers is replaced by the 413 below.
                    seen["too_large"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def _send(message: Message) -> None:
            if not seen["too_large"]:
                await send(message)
            elif not seen["refused"]:
                seen["refused"] = True
                await self._refuse(scope, receive, send)

        await self.app(scope, _receive, _send)
        if seen["too_large"] and not seen["refused"]:
            await self._refuse(scope, receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = {
            "code": "REQUEST_TOO_LARGE",
            "message": f"request body exceeds {self.max_bytes} bytes",
            "context": {"max_bytes": self.max_bytes},
        }
        await JSONResponse(status_code=413, content=body)(scope, receive, send)


class SecurityHeadersMiddleware:
    """Hardening headers on every response: no MIME sniffing, no framing, no referrer, no
    caching of API answers, a restrictive Content-Security-Policy (except on the docs pages,
    which load their own scripts) and, when API_HSTS_MAX_AGE_S is set, Strict-Transport-Security.
    """

    def __init__(self, app: ASGIApp, hsts_max_age_s: int) -> None:
        self.app = app
        self.hsts_max_age_s = hsts_max_age_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = scope.get("path", "") in DOCS_PATHS

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-store")
                if not docs:
                    headers.setdefault("Content-Security-Policy",
                                       "default-src 'none'; frame-ancestors 'none'")
                if self.hsts_max_age_s:
                    headers.setdefault("Strict-Transport-Security",
                                       f"max-age={self.hsts_max_age_s}")
            await send(message)

        await self.app(scope, receive, _send)


class RequestContextMiddleware:
    """Gives every request a correlation id (the caller's ``header`` - API_CORRELATION_HEADER -
    when it is a safe token, otherwise a new one), echoes it in the response, and records HTTP metrics labelled by
    route template (never the raw path, so ids do not multiply series)."""

    def __init__(self, app: ASGIApp, header: str) -> None:
        self.app = app
        self.header = header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = dict(scope.get("headers") or []).get(self.header.lower().encode())
        cid = correlation.sanitize(sent.decode("latin-1") if sent else None)
        cid = cid or correlation.new_correlation_id()
        token = correlation.set_correlation_id(cid)
        status = {"code": 500}
        started = time.perf_counter()

        started_response = {"sent": False}

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                started_response["sent"] = True
                status["code"] = message["status"]
                MutableHeaders(scope=message).append(self.header, cid)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            # The traceback goes to the server log under the correlation id; the caller only
            # gets the id to quote, never the exception text or stack (paths, secrets).
            logger.exception("unhandled error serving request")
            if started_response["sent"]:
                raise
            body = {
                "code": "INTERNAL_ERROR",
                "message": "internal server error",
                "context": {"correlation_id": cid},
            }
            await JSONResponse(status_code=500, content=body)(scope, receive, _send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method", "")
            metrics.HTTP_REQUESTS.labels(method, route, str(status["code"])).inc()
            metrics.HTTP_DURATION.labels(method, route).observe(time.perf_counter() - started)
            correlation.reset_correlation_id(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    configure_tracing(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The outbox dispatcher runs beside the API while NOTIFICATION_DISPATCH_ENABLED; with
        # several replicas each runs one, and the claim protocol keeps them from double-sending.
        enforce_route_policies(app)  # also covers routes added after create_app
        thread = app.state.dispatcher
        if thread is not None:
            thread.start()
        try:
            yield
        finally:
            if thread is not None:
                thread.stop(
                    settings.notification_timeout_s + settings.notification_dispatch_interval_s
                )

    docs = (settings.api_docs_enabled if settings.api_docs_enabled is not None
            else settings.environment != "production")
    app = FastAPI(
        title="O-RAN Model Adaptation Framework",
        version=__version__,
        lifespan=_lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.rate_limits = TokenBuckets(
        settings.api_rate_limit_per_minute,
        settings.api_rate_limit_burst,
        max_keys=settings.api_rate_limit_max_keys,
    )
    app.state.auth_failures = TokenBuckets(
        settings.api_auth_failure_limit_per_minute,
        settings.api_auth_failure_limit_per_minute,
        max_keys=settings.api_rate_limit_max_keys,
    )
    app.state.engine = create_db_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.registry = build_registry(settings)
    app.state.deployer = build_deployer(settings, app.state.registry)
    app.state.model_handler = build_model_handler(settings)
    app.state.llm_client = build_llm(settings)
    app.state.job_queue = build_job_queue(settings)
    app.state.auth = build_auth(settings)
    app.state.policy = build_policy(settings)
    app.state.notifiers = build_notifiers(settings)
    app.state.data_access = build_data_access(settings)
    app.state.rollout_metrics = build_rollout_metrics(settings)
    app.state.drift_mappers = load_mappers(settings.drift_mappers)
    app.state.dispatcher = (
        DispatcherThread(
            Dispatcher(app.state.session_factory, app.state.notifiers, settings)
        )
        if settings.notification_dispatch_enabled and app.state.notifiers
        else None
    )
    # Added first so it runs innermost: an oversized request still gets a correlation id and
    # is counted in the HTTP metrics by RequestContextMiddleware.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.api_max_request_bytes)
    app.add_middleware(RequestContextMiddleware, header=settings.api_correlation_header)
    # Outermost, so even the 500 answer RequestContextMiddleware writes carries the headers.
    app.add_middleware(SecurityHeadersMiddleware, hsts_max_age_s=settings.api_hsts_max_age_s)

    @app.exception_handler(AdaptationError)
    async def _adaptation_error(_: Request, exc: AdaptationError) -> JSONResponse:
        headers = None
        if exc.code == "RATE_LIMITED":
            headers = {"Retry-After": str(exc.context.get("retry_after_s", 1))}
        return JSONResponse(status_code=_status_for(exc), content=exc.to_dict(), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only where and why - never the offending input, which may carry data or secrets.
        errors = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        body = {
            "code": "INVALID_REQUEST",
            "message": "request failed validation",
            "context": {"errors": errors},
        }
        return JSONResponse(status_code=422, content=json.loads(json.dumps(body, default=str)))

    # Every caller needs the read action; write endpoints add their own.
    authenticated = [Depends(require(READ))]
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(adaptation_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(data_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(current_data_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(models_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(config_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(notifications_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(rollouts_router, prefix="/api/v1", dependencies=authenticated)
    enforce_route_policies(app)
    return app


def _status_for(exc: AdaptationError) -> int:
    return {
        "UNAUTHENTICATED": 401,
        "FORBIDDEN": 403,
        "RATE_LIMITED": 429,
        "OUTBOUND_BLOCKED": 422,
        "MODEL_NOT_FOUND": 404,
        "JOB_NOT_FOUND": 404,
        "DATASET_NOT_FOUND": 404,
        "DELIVERY_NOT_FOUND": 404,
        "ROLLOUT_NOT_FOUND": 404,
        "EVENT_MAPPER_NOT_FOUND": 404,
        "EVENT_MAPPING_FAILED": 422,
        "CONFLICT": 409,
        "DATA_VERSION_CONFLICT": 409,
        "MODEL_BUSY": 409,
        "INVALID_STATE_TRANSITION": 409,
        "JOB_NOT_CANCELLABLE": 409,
        "ROLLOUT_STATE_CONFLICT": 409,
        "ARTIFACT_INTEGRITY_FAILED": 422,
        "INVALID_REFERENCE": 422,
        "DATA_SOURCE_NOT_ALLOWED": 422,
        "DATA_FORMAT_INVALID": 422,
        "DATA_SOURCE_CHANGED": 409,
        "DATA_TOO_LARGE": 413,
        "DATA_SOURCE_UNAVAILABLE": 503,
        "MLFLOW_UNAVAILABLE": 503,
        "DEPLOYMENT_UNAVAILABLE": 503,
        "DATABASE_UNAVAILABLE": 503,
        "JOB_QUEUE_UNAVAILABLE": 503,
        "ROLLOUT_METRICS_UNAVAILABLE": 503,
        "JOB_TIMEOUT": 504,
    }.get(exc.code, 500)
