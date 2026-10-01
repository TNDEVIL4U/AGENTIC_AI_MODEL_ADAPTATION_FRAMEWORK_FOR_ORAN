"""Conformance suite for ``CdcSourcePort`` adapters (a stream of source-table changes).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
checks consume the stream the way the CDC consumer does (fetch, store with the offset in one
transaction, then ``ack``)::

    ctx = Context(session_factory=..., emit=make_changes, reopen=lambda: MySource(...))
    run(MySource(...), ctx)

``emit(n)`` makes ``n`` new row changes visible to the source; ``reopen()`` builds a new instance
on the same stream, as after a restart; ``break_source`` makes the source unreachable for the
rest of the context (None: cannot be simulated). Rules: every change arrives once consumption
is acknowledged, as a CdcEvent with a stable id; ``limit`` is honoured; an acknowledged batch is
not delivered again; a batch fetched but not stored is delivered again after a restart
(at-least-once); an unreachable source is CdcUnavailableError.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from oran_adapt.cdc.events import CdcEvent
from oran_adapt.cdc.store import store_events
from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import CdcUnavailableError
from oran_adapt.ports import CdcSourcePort

_MAX_BATCHES = 50


@dataclass
class Context:
    session_factory: Callable[[], Session]
    emit: Callable[[int], None]
    reopen: Callable[[], CdcSourcePort]
    break_source: Callable[[], None] | None = None
    batch: int = 100


def _fetch(port: CdcSourcePort, ctx: Context, limit: int) -> list[CdcEvent]:
    with ctx.session_factory() as session:
        events, _ = port.fetch(session, limit)
    return list(events)


def consume(port: CdcSourcePort, ctx: Context) -> list[CdcEvent]:
    """Fetch, store and acknowledge until the source has nothing more; every event fetched."""
    seen: list[CdcEvent] = []
    for _ in range(_MAX_BATCHES):
        with ctx.session_factory() as session:
            events, position = port.fetch(session, ctx.batch)
            store_events(session, list(events), consumer=port.name, position=position)
            session.commit()
        port.ack()
        if not events:
            return seen
        seen.extend(events)
    raise ConformanceFailure(f"the source still had events after {_MAX_BATCHES} batches")


def check_protocol(port: CdcSourcePort, ctx: Context) -> None:
    expect(isinstance(port, CdcSourcePort), "does not implement CdcSourcePort")
    expect(isinstance(port.name, str) and bool(port.name), "name must be a non-empty str")


def check_delivers_changes(port: CdcSourcePort, ctx: Context) -> None:
    consume(port, ctx)
    ctx.emit(3)
    events = consume(port, ctx)
    expect(len(events) == 3, f"3 changes must arrive as 3 events, got {len(events)}")
    expect(all(isinstance(e, CdcEvent) for e in events), "fetch must return CdcEvent objects")
    expect(len({e.event_id for e in events}) == 3, "each change must have its own event_id")
    expect(all(e.source_offset for e in events), "each event must carry its source offset")


def check_limit(port: CdcSourcePort, ctx: Context) -> None:
    consume(port, ctx)
    ctx.emit(3)
    expect(len(_fetch(port, ctx, 2)) <= 2, "fetch must return at most `limit` events")
    consume(port, ctx)


def check_acknowledged_not_redelivered(port: CdcSourcePort, ctx: Context) -> None:
    first = {e.event_id for e in consume(port, ctx)}
    ctx.emit(2)
    second = consume(port, ctx)
    expect(len(second) == 2, f"2 new changes must arrive as 2 events, got {len(second)}")
    expect(not first & {e.event_id for e in second},
           "an acknowledged event must not be delivered again")
    expect(consume(port, ctx) == [], "nothing may arrive when nothing changed")


def check_unacknowledged_redelivered(port: CdcSourcePort, ctx: Context) -> None:
    consume(port, ctx)
    ctx.emit(2)
    fetched = {e.event_id for e in _fetch(port, ctx, ctx.batch)}
    expect(len(fetched) == 2, "the new changes must be fetched")
    port.close()
    again = ctx.reopen()
    redelivered = {e.event_id for e in consume(again, ctx)}
    expect(fetched <= redelivered,
           "events fetched but never stored must be delivered again after a restart, "
           "with the same event ids")


def check_unreachable_source(port: CdcSourcePort, ctx: Context) -> None:
    if ctx.break_source is None:
        return
    ctx.break_source()
    try:
        _fetch(port, ctx, ctx.batch)
    except CdcUnavailableError:
        return
    except Exception as exc:  # any other class is the deviation reported
        raise ConformanceFailure(
            f"an unreachable source must raise CdcUnavailableError, not {type(exc).__name__}"
        ) from exc
    raise ConformanceFailure("an unreachable source must raise CdcUnavailableError")


CHECKS: dict[str, Callable[[CdcSourcePort, Context], None]] = {
    "protocol": check_protocol,
    "delivers_changes": check_delivers_changes,
    "limit": check_limit,
    "acknowledged_not_redelivered": check_acknowledged_not_redelivered,
    "unacknowledged_redelivered": check_unacknowledged_redelivered,
    # Last: it leaves the source unreachable.
    "unreachable_source": check_unreachable_source,
}


def run(port: CdcSourcePort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure. The
    ``unacknowledged_redelivered`` check closes ``port``; later checks use ``ctx.reopen()``."""
    for name, check in CHECKS.items():
        check(port, ctx)
        if name == "unacknowledged_redelivered":
            port = ctx.reopen()
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "consume", "run"]
