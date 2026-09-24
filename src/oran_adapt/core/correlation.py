"""Correlation id: one id per inbound request (or CLI/consumer run), carried through every log
line, audit row and adaptation job it causes, so one drift event can be followed end to end.

The id lives in a ContextVar. The API middleware sets it from the ``X-Correlation-ID`` request
header (or a new one), and the job wrapper copies the context into the worker thread, so code
never passes it around explicitly."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token

HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
# Caller-supplied ids end up in logs and the database: accept only short, plain tokens.
_VALID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize(value: str | None) -> str | None:
    """``value`` if it is a safe id, otherwise None (the caller then generates one)."""
    if value and _VALID.match(value):
        return value
    return None


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    _correlation_id.reset(token)


class CorrelationIdFilter(logging.Filter):
    """Stamps the current correlation id on every record that does not carry one already."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "correlation_id", None) is None:
            cid = _correlation_id.get()
            if cid is not None:
                record.correlation_id = cid
        return True
