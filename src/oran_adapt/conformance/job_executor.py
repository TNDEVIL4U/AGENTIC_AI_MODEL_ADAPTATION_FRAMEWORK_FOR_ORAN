"""Conformance suite for ``JobExecutorPort`` adapters (running one job attempt, bounded).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation::

    run(MyExecutor(...), Context(max_overrun_s=30.0))

The attempts the checks run are this module's functions, so they pickle for an executor that
runs them in another process. ``max_overrun_s`` bounds how long past its timeout an attempt may
take to be stopped (process start-up included). Rules: a result comes back unchanged; an
AdaptationError keeps its class and message; an attempt past its timeout is JobTimeoutError;
the error ``on_tick`` returns stops the attempt and is raised; a crash is raised, never
returned as a result.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import JobCancelledError, JobTimeoutError, ValidationFailedError
from oran_adapt.ports import JobCall, JobExecutorPort

_runs = itertools.count(1)


@dataclass
class Context:
    max_overrun_s: float = 30.0
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def call(self, label: str, run: Callable[[dict[str, Any]], dict[str, Any]],
             payload: dict[str, Any], timeout_s: float = 60.0, **extra: Any) -> JobCall:
        return JobCall(job_id=f"conformance{self._run:04d}{label}", timeout_s=timeout_s,
                       run=run, payload=payload, **extra)


def echo(payload: dict[str, Any]) -> dict[str, Any]:
    return {"echo": payload}


def fail_typed(payload: dict[str, Any]) -> dict[str, Any]:
    raise ValidationFailedError(payload["message"], stage=payload["stage"])


def crash(payload: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("conformance crash")


def sleep(payload: dict[str, Any]) -> dict[str, Any]:
    time.sleep(float(payload["seconds"]))
    return {"slept": payload["seconds"]}


def _raised(port: JobExecutorPort, call: JobCall) -> BaseException:
    try:
        result = port.execute(call)
    except Exception as exc:  # noqa: BLE001 - the checks inspect whatever was raised
        return exc
    raise ConformanceFailure(f"the attempt must raise, but it returned {result!r}")


def check_protocol(port: JobExecutorPort, ctx: Context) -> None:
    expect(isinstance(port, JobExecutorPort), "does not implement JobExecutorPort")
    expect(isinstance(port.in_process, bool), "in_process must be a bool")


def check_returns_result(port: JobExecutorPort, ctx: Context) -> None:
    payload = {"model": "m", "n": 3, "nested": {"ok": True, "values": [1.5, None]}}
    expect(port.execute(ctx.call("echo", echo, payload)) == {"echo": payload},
           "the attempt's result must come back unchanged")


def check_typed_errors(port: JobExecutorPort, ctx: Context) -> None:
    exc = _raised(port, ctx.call("typed", fail_typed,
                                 {"message": "held-out rows too few", "stage": "VALIDATING"}))
    expect(type(exc) is ValidationFailedError,
           f"an AdaptationError must keep its class, got {type(exc).__name__}")
    assert isinstance(exc, ValidationFailedError)
    expect(exc.message == "held-out rows too few", "an AdaptationError must keep its message")


def check_crash_raised(port: JobExecutorPort, ctx: Context) -> None:
    exc = _raised(port, ctx.call("crash", crash, {}))
    expect("conformance crash" in str(exc), "a crash must be raised with its message")


def check_timeout(port: JobExecutorPort, ctx: Context) -> None:
    timeout_s = 0.5
    started = time.monotonic()
    exc = _raised(port, ctx.call("slow", sleep, {"seconds": 3}, timeout_s=timeout_s))
    took = time.monotonic() - started
    expect(isinstance(exc, JobTimeoutError),
           f"an attempt past its timeout must raise JobTimeoutError, not {type(exc).__name__}")
    expect(took < timeout_s + ctx.max_overrun_s,
           f"a timed-out attempt must be stopped within {ctx.max_overrun_s}s, took {took:.1f}s")


def check_tick_stops(port: JobExecutorPort, ctx: Context) -> None:
    stop = JobCancelledError("cancel requested by the conformance suite")
    ticks: list[float] = []

    def on_tick() -> Exception | None:
        ticks.append(time.monotonic())
        return stop if len(ticks) >= 2 else None

    started = time.monotonic()
    exc = _raised(port, ctx.call("tick", sleep, {"seconds": 3}, tick_s=0.1, on_tick=on_tick))
    expect(exc is stop, f"the error on_tick returns must be raised, got {type(exc).__name__}")
    expect(len(ticks) == 2, f"on_tick must not be called after it stopped the attempt ({ticks})")
    expect(time.monotonic() - started < ctx.max_overrun_s,
           "a stopped attempt must end within max_overrun_s")


CHECKS: dict[str, Callable[[JobExecutorPort, Context], None]] = {
    "protocol": check_protocol,
    "returns_result": check_returns_result,
    "typed_errors": check_typed_errors,
    "crash_raised": check_crash_raised,
    "timeout": check_timeout,
    "tick_stops": check_tick_stops,
}


def run(port: JobExecutorPort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
