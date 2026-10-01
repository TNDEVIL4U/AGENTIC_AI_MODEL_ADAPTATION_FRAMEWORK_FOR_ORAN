"""Conformance suite for ``RolloutMetricsPort`` adapters (a rollout's online health metrics).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
context's ``seed(window, at, requests, metrics)`` makes the metrics source hold one observation
of ``window``'s arm at time ``at`` (a stored row, a scraped sample in a test double), and
``break_source`` makes the source unreachable::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_source(check, session):
        CHECKS[check](MySource(...), Context(session=session, seed=..., break_source=...))

docs/adapters/rollout_metrics.md explains each rule.
"""

from __future__ import annotations

import itertools
import math
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import RolloutMetricsUnavailableError
from oran_adapt.ports import ArmStats, ArmWindow, RolloutMetricsPort

_runs = itertools.count(1)
_EPOCH = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass
class Context:
    session: Any
    """The database session ``observe`` receives."""
    seed: Callable[[ArmWindow, datetime, int, dict[str, float]], None]
    """``seed(window, at, requests, metrics)``: one observation of the window's arm at ``at``."""
    break_source: Callable[[], None] | None = None
    """Make the metrics source unreachable for the rest of this context (None: cannot be
    simulated)."""
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def window(self, label: str, arm: str = "candidate", minutes: int = 10) -> ArmWindow:
        start = _EPOCH + timedelta(hours=self._run)
        return ArmWindow(
            rollout_id=f"conformance{self._run:04d}{label}", model=f"conformance-{label}",
            arm=arm, version="2" if arm == "candidate" else "1", start=start,
            end=start + timedelta(minutes=minutes),
        )


def _stats(port: RolloutMetricsPort, ctx: Context, window: ArmWindow) -> ArmStats:
    stats = port.observe(ctx.session, window)
    expect(isinstance(stats, ArmStats), f"observe returned {type(stats).__name__}, not ArmStats")
    expect(isinstance(stats.count, int) and stats.count >= 0,
           f"count must be a non-negative int, got {stats.count!r}")
    for name, values in stats.samples.items():
        expect(isinstance(name, str) and name != "", f"metric name {name!r} is not a name")
        expect(all(isinstance(v, float) and math.isfinite(v) for v in values),
               f"samples of {name} must be finite floats: {values!r}")
    return stats


def check_protocol(port: RolloutMetricsPort, ctx: Context) -> None:
    expect(isinstance(port, RolloutMetricsPort), "does not implement RolloutMetricsPort")
    port.ping()


def check_empty_window(port: RolloutMetricsPort, ctx: Context) -> None:
    stats = _stats(port, ctx, ctx.window("empty"))
    expect(stats.count == 0, f"a window nothing was observed in counts {stats.count} requests")
    expect(not any(stats.samples.values()), f"a window with no observations has {stats.samples}")


def check_reads_back(port: RolloutMetricsPort, ctx: Context) -> None:
    window = ctx.window("read")
    middle = window.start + (window.end - window.start) / 2
    ctx.seed(window, middle, 5, {"latency_ms": 12.5, "error_rate": 0.0})
    ctx.seed(window, middle + timedelta(seconds=30), 7, {"latency_ms": 14.5})
    stats = _stats(port, ctx, window)
    expect(stats.count >= 1, "seeded observations were not counted")
    latency = stats.samples.get("latency_ms") or []
    expect(sorted(set(latency)) == [12.5, 14.5],
           f"latency_ms samples read back as {latency}, expected 12.5 and 14.5")
    expect(0.0 in (stats.samples.get("error_rate") or []), "error_rate 0.0 was not read back")


def check_arms_separate(port: RolloutMetricsPort, ctx: Context) -> None:
    candidate = ctx.window("arms", arm="candidate")
    stable = ctx.window("arms", arm="stable")
    middle = candidate.start + timedelta(minutes=1)
    ctx.seed(stable, middle, 3, {"latency_ms": 99.0})
    ctx.seed(candidate, middle, 3, {"latency_ms": 11.0})
    c_values = _stats(port, ctx, candidate).samples.get("latency_ms") or []
    s_values = _stats(port, ctx, stable).samples.get("latency_ms") or []
    expect(99.0 not in c_values, "the stable arm's samples were read as the candidate's")
    expect(11.0 not in s_values, "the candidate arm's samples were read as the stable arm's")
    expect(11.0 in c_values and 99.0 in s_values, "an arm's own samples were not read back")


def check_window_bounds(port: RolloutMetricsPort, ctx: Context) -> None:
    window = ctx.window("bounds")
    ctx.seed(window, window.start - timedelta(hours=1), 4, {"latency_ms": 500.0})
    ctx.seed(window, window.end + timedelta(hours=1), 4, {"latency_ms": 700.0})
    ctx.seed(window, window.start + timedelta(minutes=2), 4, {"latency_ms": 20.0})
    values = _stats(port, ctx, window).samples.get("latency_ms") or []
    expect(500.0 not in values and 700.0 not in values,
           f"observations outside the window were read: {values}")
    expect(20.0 in values, "the observation inside the window was not read")


def check_pickle(port: RolloutMetricsPort, ctx: Context) -> None:
    """The controller runs in worker processes: the adapter must survive pickling."""
    copy = pickle.loads(pickle.dumps(port))
    _stats(copy, ctx, ctx.window("pickle"))


def check_unreachable_source(port: RolloutMetricsPort, ctx: Context) -> None:
    if ctx.break_source is None:
        return
    ctx.break_source()
    window = ctx.window("down")
    for action in (port.ping, lambda: port.observe(ctx.session, window)):
        try:
            action()
        except RolloutMetricsUnavailableError:
            continue
        except Exception as exc:  # any other class is the deviation reported
            raise ConformanceFailure(
                f"an unreachable source must raise RolloutMetricsUnavailableError, not "
                f"{type(exc).__name__}"
            ) from exc
        raise ConformanceFailure(
            "an unreachable source must raise RolloutMetricsUnavailableError (ping and observe)"
        )


CHECKS: dict[str, Callable[[RolloutMetricsPort, Context], None]] = {
    "protocol": check_protocol,
    "empty_window": check_empty_window,
    "reads_back": check_reads_back,
    "arms_separate": check_arms_separate,
    "window_bounds": check_window_bounds,
    "pickle": check_pickle,
    # Last: it leaves the source unreachable.
    "unreachable_source": check_unreachable_source,
}


def run(port: RolloutMetricsPort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
