"""Adapter calls, measured: latency, errors and a span for every call to a port's adapter.

``observe(adapter, port=..., name=...)`` wraps each public method of the adapter's class on the
instance itself, so the adapter keeps its type (``isinstance`` checks and runtime-checkable
ports still hold) and stays picklable (a job's attempt process receives the registry). Each
call records ``adapter_call_duration_seconds`` and, when it raises, ``adapter_errors_total``
with the error's code, and runs inside a ``<port>.<operation>`` span. A call the adapter makes
to itself is not counted again: only the outermost call on a port is measured.
"""

from __future__ import annotations

import inspect
import time
from contextvars import ContextVar
from typing import Any, TypeVar

from oran_adapt.core import metrics, tracing
from oran_adapt.core.errors import AdaptationError

T = TypeVar("T")
_ATTR = "_oran_observed"
_inside: ContextVar[frozenset[str]] = ContextVar("oran_adapt_adapter_calls", default=frozenset())


class _Timed:
    """The measured stand-in for one method; calls the class's method at call time, so a
    method patched on the class later is still the one called."""

    def __init__(self, target: Any, port: str, adapter: str, operation: str) -> None:
        self.target = target
        self.port = port
        self.adapter = adapter
        self.operation = operation

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        method = getattr(type(self.target), self.operation)
        active = _inside.get()
        if self.port in active:
            return method(self.target, *args, **kwargs)
        token = _inside.set(active | {self.port})
        started = time.perf_counter()
        try:
            with tracing.span(f"{self.port}.{self.operation}", port=self.port,
                              adapter=self.adapter):
                return method(self.target, *args, **kwargs)
        except Exception as exc:
            code = exc.code if isinstance(exc, AdaptationError) else "UNEXPECTED"
            metrics.ADAPTER_ERRORS.labels(self.port, self.adapter, self.operation, code).inc()
            raise
        finally:
            _inside.reset(token)
            metrics.ADAPTER_CALL_DURATION.labels(self.port, self.adapter, self.operation).observe(
                time.perf_counter() - started
            )


def operations(cls: type) -> list[str]:
    """The public plain methods of ``cls`` (no properties, class or static methods)."""
    return sorted(
        name for name in dir(cls)
        if not name.startswith("_")
        and inspect.isfunction(inspect.getattr_static(cls, name, None))
    )


def observe(adapter: T, *, port: str, name: str) -> T:
    """Measure every call to ``adapter`` (the adapter ``name`` of ``port``). Idempotent; an
    object that does not take instance attributes is returned as it is."""
    if adapter is None or getattr(adapter, _ATTR, False):
        return adapter
    try:
        for operation in operations(type(adapter)):
            setattr(adapter, operation, _Timed(adapter, port, name, operation))
        setattr(adapter, _ATTR, True)
    except (AttributeError, TypeError):
        for operation in operations(type(adapter)):
            try:
                delattr(adapter, operation)
            except (AttributeError, TypeError):
                continue
    return adapter
