"""Structured (JSON) logging carrying the adaptation context fields."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

CONTEXT_FIELDS = (
    "adaptation_job_id", "model_id", "model_version", "strategy",
    "component", "status", "error",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for f in CONTEXT_FIELDS:
            if hasattr(record, f):
                payload[f] = getattr(record, f)
        if record.exc_info:
            payload["error"] = payload.get("error") or self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_oran_adapt", False):
            root.removeHandler(h)
    handler = logging.StreamHandler()
    handler._oran_adapt = True  # type: ignore[attr-defined]
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(
    logger: logging.Logger, message: str, level: int = logging.INFO, **ctx: object
) -> None:
    """Log with the standard context fields (job id, model, strategy, component, status...)."""
    logger.log(level, message, extra={k: v for k, v in ctx.items() if k in CONTEXT_FIELDS})
