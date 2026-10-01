"""Structured (JSON) logging carrying the adaptation context fields.

Every line carries the correlation id and, inside a span, the ``trace_id`` and ``span_id`` of
core.tracing, so a log line leads to its trace. Messages and errors are redacted before they
are written: credentials in URLs (``scheme://user:secret@``), ``password=``/``token=``/
``secret=``/``api_key=``-style assignments and bearer tokens become ``***``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from oran_adapt.core import tracing
from oran_adapt.core.correlation import CorrelationIdFilter

CONTEXT_FIELDS = (
    "correlation_id", "adaptation_job_id", "model_id", "model_version", "strategy",
    "component", "status", "error", "event_id", "event_type", "sink", "delivery_id",
    "trace_id", "span_id",
)
MASK = "***"
_SECRET_PATTERNS = (
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\1{MASK}:{MASK}@"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|client_secret|api[_-]?key|"
                r"access[_-]?key|private[_-]?key)(\"?'?\s*[=:]\s*\"?'?)([^\s\"'&,;]+)"),
     rf"\1\2{MASK}"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+"), f"Bearer {MASK}"),
)


def redact(text: str) -> str:
    """``text`` with the credentials it may carry replaced by ``***`` (module docstring)."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class TraceIdFilter(logging.Filter):
    """Stamps the current span's trace and span ids on each record (none outside a span)."""

    def filter(self, record: logging.LogRecord) -> bool:
        ids = tracing.current_ids()
        if ids is not None:
            record.trace_id, record.span_id = ids
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for f in CONTEXT_FIELDS:
            if hasattr(record, f):
                payload[f] = getattr(record, f)
        if record.exc_info:
            payload["error"] = payload.get("error") or self.formatException(record.exc_info)
        if isinstance(payload.get("error"), str):
            payload["error"] = redact(payload["error"])
        return json.dumps(payload, default=str)


class RedactingFormatter(logging.Formatter):
    """The plain-text format (LOG_JSON=false), redacted like the JSON one."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_oran_adapt", False):
            root.removeHandler(h)
    handler = logging.StreamHandler()
    handler._oran_adapt = True  # type: ignore[attr-defined]
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(TraceIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(
    logger: logging.Logger, message: str, level: int = logging.INFO, **ctx: object
) -> None:
    """Log with the standard context fields (job id, model, strategy, component, status...)."""
    logger.log(level, message, extra={k: v for k, v in ctx.items() if k in CONTEXT_FIELDS})
