"""The OpenTelemetry SDK behind core.tracing: sampler, keyed ids and the span exporter.

TRACING_EXPORTER picks the exporter:

    none     nothing is installed; the API stays a no-op (the default)
    console  every span as JSON on stderr
    jsonl    one JSON line per span appended to TRACING_JSONL_PATH. Every process of a
             deployment (API, worker, each job's attempt process) may append to the same file,
             one write per span, so a trace can be read back whole without a collector.
    otlp     OTLP over HTTP to TRACING_OTLP_ENDPOINT, batched. Needs the
             opentelemetry-exporter-otlp-proto-http package, which is not a dependency; without
             it this fails at startup with a ConfigurationError naming the key.

The sampler is parent-based with TRACING_SAMPLE_RATIO at the root. Root trace ids are keyed
(core.tracing), so every process decides the same way for the same event.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from oran_adapt.core import tracing
from oran_adapt.core.errors import ConfigurationError

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

OTLP_MODULE = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


class KeyedIdGenerator(RandomIdGenerator):
    """Random ids, except a root span started by core.tracing.event_span takes its event's
    keyed trace id."""

    def generate_trace_id(self) -> int:
        return tracing.next_trace_id() or super().generate_trace_id()


class JsonlSpanExporter(SpanExporter):
    """Appends each span as one JSON line: trace and span ids, parent, name, kind, times,
    status, attributes, links and the service name."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    @staticmethod
    def to_record(span: ReadableSpan) -> dict:
        ctx = span.get_span_context()
        assert ctx is not None
        return {
            "trace_id": f"{ctx.trace_id:032x}",
            "span_id": f"{ctx.span_id:016x}",
            "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
            "name": span.name,
            "kind": span.kind.name,
            "start_ns": span.start_time,
            "end_ns": span.end_time,
            "status": span.status.status_code.name,
            "attributes": dict(span.attributes or {}),
            "links": [f"{link.context.trace_id:032x}:{link.context.span_id:016x}"
                      for link in span.links],
            "service": span.resource.attributes.get("service.name"),
            "pid": os.getpid(),
        }

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = "".join(json.dumps(self.to_record(s), default=str) + "\n" for s in spans)
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(lines)
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _otlp(settings: Settings) -> SpanExporter:
    try:
        module = importlib.import_module(OTLP_MODULE)
    except ImportError as exc:
        raise ConfigurationError(
            "TRACING_EXPORTER=otlp needs the opentelemetry-exporter-otlp-proto-http package",
            key="TRACING_EXPORTER",
        ) from exc
    exporter: SpanExporter = module.OTLPSpanExporter(
        endpoint=settings.tracing_otlp_endpoint, timeout=settings.tracing_otlp_timeout_s
    )
    return exporter


_configured: tuple | None = None


def configure(settings: Settings) -> bool:
    """Install the provider TRACING_* describes on core.tracing (once per process and
    configuration). Returns whether spans are recorded."""
    global _configured
    key = (settings.tracing_exporter, settings.tracing_service_name,
           settings.tracing_sample_ratio, settings.tracing_jsonl_path,
           settings.tracing_otlp_endpoint)
    if key == _configured:
        return settings.tracing_exporter != "none"
    old = tracing.provider()
    if settings.tracing_exporter == "none":
        tracing.set_provider(None)
    else:
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.tracing_service_name}),
            sampler=ParentBased(TraceIdRatioBased(settings.tracing_sample_ratio)),
            id_generator=KeyedIdGenerator(),
        )
        if settings.tracing_exporter == "otlp":
            provider.add_span_processor(BatchSpanProcessor(_otlp(settings)))
        elif settings.tracing_exporter == "jsonl":
            assert settings.tracing_jsonl_path is not None  # checked by Settings
            provider.add_span_processor(
                SimpleSpanProcessor(JsonlSpanExporter(settings.tracing_jsonl_path))
            )
        else:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        tracing.set_provider(provider)
    shutdown = getattr(old, "shutdown", None)
    if shutdown is not None:
        shutdown()
    _configured = key
    return settings.tracing_exporter != "none"
