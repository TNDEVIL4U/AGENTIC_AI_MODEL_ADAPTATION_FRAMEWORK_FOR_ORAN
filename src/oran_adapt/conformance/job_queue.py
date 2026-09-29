"""Conformance suite for ``JobQueuePort`` adapters (waking workers for queued jobs).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
context reads back what reached the broker and can make the broker unreachable::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_queue(check):
        CHECKS[check](MyQueue(...), Context(delivered=..., break_broker=...))

An adapter without a broker (``database``, ``inline``) passes ``delivered=None``: publishing
must then be a harmless no-op. docs/adapters/job_queue.md explains each rule.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import JobQueueUnavailableError
from oran_adapt.ports import JobQueuePort, QueuedJob

_runs = itertools.count(1)


@dataclass
class Context:
    delivered: Callable[[], list[tuple[str, str]]] | None
    """``delivered()``: the (job_id, worker_class) of every message the broker received so far,
    in order. None for an adapter without a broker."""
    break_broker: Callable[[], None] | None = None
    """Make the broker unreachable for the rest of this context (None: cannot be simulated)."""
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def job(self, label: str, worker_class: str = "default", attempt: int = 0) -> QueuedJob:
        return QueuedJob(job_id=f"conformance{self._run:04d}{label}", worker_class=worker_class,
                         tenant="default", priority=0, attempt=attempt)


def _received(ctx: Context, job_id: str) -> list[tuple[str, str]]:
    assert ctx.delivered is not None
    return [d for d in ctx.delivered() if d[0] == job_id]


def check_protocol(port: JobQueuePort, ctx: Context) -> None:
    expect(isinstance(port, JobQueuePort), "does not implement JobQueuePort")
    expect(isinstance(port.runs_inline, bool), "runs_inline must be a bool")
    port.ping()


def check_publish_delivers(port: JobQueuePort, ctx: Context) -> None:
    job = ctx.job("deliver")
    port.publish(job)
    if ctx.delivered is None:
        return
    expect(len(_received(ctx, job.job_id)) == 1,
           "a published job must reach the broker exactly once per publish")


def check_routes_by_worker_class(port: JobQueuePort, ctx: Context) -> None:
    if ctx.delivered is None:
        return
    gpu, cpu = ctx.job("gpu", worker_class="gpu"), ctx.job("cpu", worker_class="default")
    port.publish(gpu)
    port.publish(cpu)
    expect(_received(ctx, gpu.job_id) == [(gpu.job_id, "gpu")],
           "a job must be routed to its worker class's queue")
    expect(_received(ctx, cpu.job_id) == [(cpu.job_id, "default")],
           "a job must be routed to its worker class's queue")


def check_republish_is_safe(port: JobQueuePort, ctx: Context) -> None:
    """The reaper publishes an unclaimed job again: that must not raise (the claim, not the
    broker, keeps a job from running twice)."""
    job = ctx.job("again")
    port.publish(job)
    port.publish(job)
    port.publish(ctx.job("again", attempt=1))


def check_unreachable_broker(port: JobQueuePort, ctx: Context) -> None:
    if ctx.break_broker is None:
        return
    ctx.break_broker()
    for action in (lambda: port.publish(ctx.job("down")), port.ping):
        try:
            action()
        except JobQueueUnavailableError:
            continue
        except Exception as exc:  # any other class is the deviation reported
            raise ConformanceFailure(
                f"an unreachable broker must raise JobQueueUnavailableError, not "
                f"{type(exc).__name__}"
            ) from exc
        raise ConformanceFailure("an unreachable broker must raise JobQueueUnavailableError")


CHECKS: dict[str, Callable[[JobQueuePort, Context], None]] = {
    "protocol": check_protocol,
    "publish_delivers": check_publish_delivers,
    "routes_by_worker_class": check_routes_by_worker_class,
    "republish_is_safe": check_republish_is_safe,
    # Last: it leaves the broker unreachable.
    "unreachable_broker": check_unreachable_broker,
}


def run(port: JobQueuePort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
