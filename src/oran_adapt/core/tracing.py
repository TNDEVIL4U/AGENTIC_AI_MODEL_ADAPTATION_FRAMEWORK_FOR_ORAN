"""Distributed tracing: one trace per drift event, from intake to deployment verification.

Only the OpenTelemetry *API* is used here. The SDK, the sampler and the exporter are chosen at
the composition root (bootstrap.configure_tracing, TRACING_EXPORTER) and installed with
``set_provider``; until then, and with TRACING_EXPORTER=none, every span is a no-op.

Keyed traces. The trace id of an event is derived from its key (``event_id`` when the caller
gave one, else ``DriftEvent.idempotency_key()``): the first 128 bits of
sha256("oran-adapt:" + key). The id of an event's trace can therefore be computed from the
event alone (``trace_id_hex``), and a duplicate submission of the same event lands in the same
trace. The configured provider's id generator reads the key from ``next_trace_id`` when it
starts the intake span, which is a true root span.

Propagation. The intake span's W3C ``traceparent`` is stored on the job row
(``adaptation_job.trace_context``) and sent on the broker message (``QueuedJob.trace_context``).
The worker continues the trace from the row - the message only wakes it - and hands the
attempt span's context to the attempt, in this process or a fresh one, through the job payload.
Each pipeline stage is a span (``StageSpans``), every adapter call inside it is one
(core.observed), and the deployment read-back is ``deployment.verify``. Rollout ticks continue
the trace of the job that started the rollout.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode, TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACER_NAME = "oran_adapt"
_PROPAGATOR = TraceContextTextMapPropagator()
_provider: TracerProvider | None = None
# The trace id the next root span must take (read by the provider's id generator).
_next_trace_id: ContextVar[int | None] = ContextVar("oran_adapt_next_trace_id", default=None)


def set_provider(provider: TracerProvider | None) -> None:
    """Install the provider every oran-adapt span is created with (None: back to the global
    OpenTelemetry provider, a no-op unless the process installed one)."""
    global _provider
    _provider = provider


def provider() -> TracerProvider | None:
    return _provider


def tracer() -> trace.Tracer:
    if _provider is not None:
        return _provider.get_tracer(TRACER_NAME)
    return trace.get_tracer(TRACER_NAME)


def flush(timeout_ms: int = 5000) -> None:
    """Export every finished span now (a worker process calls this before it returns)."""
    force = getattr(_provider, "force_flush", None)
    if force is not None:
        force(timeout_ms)


def trace_id_for(key: str) -> int:
    """The 128-bit trace id of the event keyed ``key`` (never 0, the invalid id)."""
    value = int(hashlib.sha256(f"oran-adapt:{key}".encode()).hexdigest()[:32], 16)
    return value or 1


def trace_id_hex(key: str) -> str:
    return f"{trace_id_for(key):032x}"


def next_trace_id() -> int | None:
    """The keyed trace id a root span being started now must use, if any."""
    return _next_trace_id.get()


def _attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    return {f"oran.{k}": v if isinstance(v, bool | int | float) else str(v)
            for k, v in attrs.items() if v is not None}


def inject() -> str | None:
    """The current span's W3C traceparent, or None when there is no valid span."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def _context_of(traceparent: str | None) -> otel_context.Context | None:
    if not traceparent:
        return None
    ctx = _PROPAGATOR.extract({"traceparent": traceparent})
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


@contextmanager
def _use(span: Span) -> Iterator[Span]:
    with trace.use_span(span, end_on_exit=True, record_exception=True,
                        set_status_on_exception=True):
        yield span


@contextmanager
def event_span(name: str, key: str, *, kind: SpanKind = SpanKind.SERVER,
               **attrs: Any) -> Iterator[Span]:
    """A root span in the trace keyed ``key``. A caller's own current span (an instrumented
    client) is kept as a link, since the event's trace id wins."""
    caller = trace.get_current_span().get_span_context()
    links = [Link(caller)] if caller.is_valid else []
    token = _next_trace_id.set(trace_id_for(key))
    try:
        span = tracer().start_span(name, context=otel_context.Context(), kind=kind,
                                   links=links, attributes=_attributes(attrs))
    finally:
        _next_trace_id.reset(token)
    with _use(span):
        yield span


@contextmanager
def continued(name: str, traceparent: str | None, *, key: str,
              kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[Span]:
    """A span continuing the trace ``traceparent`` names; without one (a job recorded before
    traces were stored) a root span of the trace keyed ``key``."""
    parent = _context_of(traceparent)
    if parent is None:
        with event_span(name, key, kind=kind, **attrs) as span:
            yield span
        return
    span = tracer().start_span(name, context=parent, kind=kind, attributes=_attributes(attrs))
    with _use(span):
        yield span


@contextmanager
def span(name: str, *, kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[Span]:
    """A child of the current span. An exception escaping it is recorded on it."""
    with tracer().start_as_current_span(name, kind=kind, attributes=_attributes(attrs),
                                        record_exception=True,
                                        set_status_on_exception=True) as current:
        yield current


def current_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) of the current span as hex, for log lines; None outside a span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


class StageSpans:
    """One span per pipeline stage. ``enter`` ends the previous stage's span and makes the new
    one current, so what the stage calls nests under it; ``close`` ends the last one.
    ``on_stage_end(stage, seconds, outcome)`` is told how long each stage took."""

    def __init__(self, on_stage_end: Callable[[str, float, str], None] | None = None) -> None:
        self._on_stage_end = on_stage_end
        self._span: Span | None = None
        self._token: Token[otel_context.Context] | None = None
        self._stage: str | None = None
        self._started = 0.0

    def enter(self, stage: str) -> None:
        self.close()
        self._span = tracer().start_span(f"stage {stage}", attributes=_attributes({"stage": stage}))
        self._token = otel_context.attach(trace.set_span_in_context(self._span))
        self._stage = stage
        self._started = time.perf_counter()

    def close(self, error: BaseException | None = None) -> None:
        if self._span is None:
            return
        span_, token, stage = self._span, self._token, self._stage
        self._span, self._token, self._stage = None, None, None
        if token is not None:
            otel_context.detach(token)
        if error is not None:
            span_.record_exception(error)
            span_.set_status(Status(StatusCode.ERROR, type(error).__name__))
        span_.end()
        if self._on_stage_end is not None and stage is not None:
            self._on_stage_end(stage, time.perf_counter() - self._started,
                               "ok" if error is None else "error")
