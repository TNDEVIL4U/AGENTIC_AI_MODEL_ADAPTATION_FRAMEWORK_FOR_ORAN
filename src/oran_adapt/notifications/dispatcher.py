"""Delivering the outbox to the sinks.

Each poll claims up to NOTIFICATION_DISPATCH_BATCH due deliveries. A claim is a conditional
UPDATE (``status = PENDING AND next_attempt_at <= now``) that pushes ``next_attempt_at`` to the
end of a lease, so two dispatchers never both win a row, and a row whose dispatcher dies
mid-send becomes due again once the lease runs out: nothing is lost, and the resend carries the
same event id. The attempt is counted at the claim, so a message that crashes its dispatcher
still runs out of attempts.

The sink is called outside any transaction. Then, only if the row is still ours:
- accepted: DELIVERED;
- refused with ``retryable`` (down, timeout, 5xx, 429): due again after an exponential backoff
  with jitter, or DEAD once NOTIFICATION_MAX_ATTEMPTS is reached;
- refused for good (bad signature, bad request): DEAD at once.

DEAD rows stay until redriven (oran_adapt.notifications.service). A per-sink circuit breaker
stops calling a sink after NOTIFICATION_BREAKER_FAILURES consecutive failures; after
NOTIFICATION_BREAKER_RESET_S one trial delivery decides whether it closes again. While it is
open, that sink's deliveries simply wait (no attempt is spent).
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select, update

from oran_adapt.core import metrics
from oran_adapt.core.errors import NotificationDeliveryError
from oran_adapt.core.logging import log_event
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import NotificationDelivery, NotificationEvent
from oran_adapt.notifications.events import DEAD, DELIVERED, PENDING, body_of
from oran_adapt.notifications.signing import parse_keys, sign
from oran_adapt.ports import OutboundMessage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import NotificationPort

logger = logging.getLogger("oran_adapt.notifications")

_ERROR_TEXT_LIMIT = 2000


@dataclass
class _SinkCircuit:
    failures: int = 0
    opened_at: float | None = None
    trial: bool = False


class CircuitBreaker:
    """Consecutive-failure breaker per sink: closed, open, then half-open (one trial)."""

    def __init__(self, threshold: int, reset_s: float, clock: Callable[[], float]) -> None:
        self._threshold = threshold
        self._reset_s = reset_s
        self._clock = clock
        self._sinks: dict[str, _SinkCircuit] = {}
        self._lock = threading.Lock()

    def _get(self, sink: str) -> _SinkCircuit:
        return self._sinks.setdefault(sink, _SinkCircuit())

    def is_open(self, sink: str) -> bool:
        """True while ``sink`` may not be called at all (open, and not yet due a trial)."""
        with self._lock:
            c = self._get(sink)
            if c.opened_at is None:
                return False
            return c.trial or self._clock() - c.opened_at < self._reset_s

    def allow(self, sink: str) -> bool:
        """Whether a call to ``sink`` may go ahead now; the first call after the reset period
        is the trial, and further calls wait for its result."""
        with self._lock:
            c = self._get(sink)
            if c.opened_at is None:
                return True
            if c.trial or self._clock() - c.opened_at < self._reset_s:
                return False
            c.trial = True
            return True

    def success(self, sink: str) -> None:
        with self._lock:
            self._sinks[sink] = _SinkCircuit()
        metrics.NOTIFICATION_CIRCUIT_OPEN.labels(sink).set(0)

    def failure(self, sink: str) -> None:
        with self._lock:
            c = self._get(sink)
            c.failures += 1
            opened = c.trial or c.failures >= self._threshold
            if opened:
                c.opened_at = self._clock()
                c.trial = False
        if opened:
            metrics.NOTIFICATION_CIRCUIT_OPEN.labels(sink).set(1)
            log_event(logger, "notification sink circuit open", logging.WARNING, sink=sink)

    def wait_s(self, sink: str) -> float:
        """Seconds until an open circuit is due its trial (a full reset period while a trial
        is in flight, 0 when closed)."""
        with self._lock:
            c = self._get(sink)
            if c.opened_at is None:
                return 0.0
            if c.trial:
                return self._reset_s
            return max(0.0, c.opened_at + self._reset_s - self._clock())


def _now() -> datetime:
    return datetime.now(UTC)


class Dispatcher:
    """Delivers due outbox rows to ``sinks`` (name -> adapter). Safe to run in several
    processes at once; one instance is used by one thread."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sinks: dict[str, NotificationPort],
        settings: Settings,
        *,
        worker_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._sessions = session_factory
        self._sinks = sinks
        self._settings = settings
        self._rng = rng
        self._keys = (
            parse_keys(settings.notification_signing_keys.get_secret_value(),
                       settings.notification_signing_min_key_bytes)
            if settings.notification_signing_keys is not None
            else []
        )
        self.worker_id = (
            worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )[:100]
        self.breaker = CircuitBreaker(
            settings.notification_breaker_failures, settings.notification_breaker_reset_s, clock
        )

    # ---- claiming ------------------------------------------------------------------------
    def _claim(self) -> list[int]:
        usable = [s for s in self._sinks if not self.breaker.is_open(s)]
        if not usable:
            return []
        now = _now()
        lease_end = now + timedelta(seconds=self._settings.notification_lease_s)
        claimed: list[int] = []
        with session_scope(self._sessions) as session:
            candidates = session.scalars(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.status == PENDING,
                    NotificationDelivery.next_attempt_at <= now,
                    NotificationDelivery.sink.in_(usable),
                )
                .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
                .limit(self._settings.notification_dispatch_batch)
            ).all()
            for delivery_id in candidates:
                result = cast(
                    CursorResult,
                    session.execute(
                        update(NotificationDelivery)
                        .where(
                            NotificationDelivery.id == delivery_id,
                            NotificationDelivery.status == PENDING,
                            NotificationDelivery.next_attempt_at <= now,
                        )
                        .values(
                            next_attempt_at=lease_end,
                            lease_until=lease_end,
                            leased_by=self.worker_id,
                            attempts=NotificationDelivery.attempts + 1,
                            updated_at=now,
                        )
                    ),
                )
                if result.rowcount == 1:
                    claimed.append(delivery_id)
        return claimed

    def _release(self, delivery_id: int, due: datetime) -> None:
        """Hand back a claimed row unsent (its sink's circuit is open): no attempt spent."""
        with session_scope(self._sessions) as session:
            session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id,
                       NotificationDelivery.leased_by == self.worker_id)
                .values(next_attempt_at=due, lease_until=None, leased_by=None,
                        attempts=NotificationDelivery.attempts - 1, updated_at=_now())
            )

    # ---- one delivery --------------------------------------------------------------------
    def _message(self, delivery_id: int) -> tuple[str, OutboundMessage]:
        with session_scope(self._sessions) as session:
            row = session.execute(
                select(NotificationDelivery.sink, NotificationDelivery.attempts,
                       NotificationEvent.event_id, NotificationEvent.event_type,
                       NotificationEvent.subject, NotificationEvent.envelope)
                .join(NotificationEvent,
                      NotificationEvent.event_id == NotificationDelivery.event_id)
                .where(NotificationDelivery.id == delivery_id)
            ).one()
        body = body_of(row.envelope)
        return row.sink, OutboundMessage(
            event_id=row.event_id,
            event_type=row.event_type,
            subject=row.subject,
            envelope=row.envelope,
            body=body,
            headers=sign(row.event_id, body, self._keys),
            attempt=row.attempts,
        )

    def _backoff_s(self, attempts: int) -> float:
        s = self._settings
        base = min(s.notification_backoff_max_s,
                   s.notification_backoff_initial_s * 2 ** max(attempts - 1, 0))
        return max(0.0, base * (1 + s.notification_backoff_jitter * (2 * self._rng() - 1)))

    def _finish(self, delivery_id: int, **values: object) -> bool:
        """Record the outcome, only if the row is still ours (not re-claimed after a lease
        ran out). True when recorded."""
        with session_scope(self._sessions) as session:
            result = cast(
                CursorResult,
                session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id,
                           NotificationDelivery.leased_by == self.worker_id,
                           NotificationDelivery.status == PENDING)
                    .values(lease_until=None, leased_by=None, updated_at=_now(), **values)
                ),
            )
            return result.rowcount == 1

    def _deliver(self, delivery_id: int) -> None:
        sink_name, message = self._message(delivery_id)
        if not self.breaker.allow(sink_name):
            self._release(delivery_id,
                          _now() + timedelta(seconds=self.breaker.wait_s(sink_name)))
            return
        sink = self._sinks[sink_name]
        started = time.perf_counter()
        error: NotificationDeliveryError | None = None
        try:
            sink.send(message)
        except NotificationDeliveryError as exc:
            error = exc
        except Exception as exc:  # an adapter broke the port contract: record it, retry
            logger.exception("notification sink %s raised %s", sink_name, type(exc).__name__)
            error = NotificationDeliveryError(
                f"{type(exc).__name__}: {exc}", retryable=True, sink=sink_name
            )
        metrics.NOTIFICATION_DELIVERY_DURATION.labels(sink_name).observe(
            time.perf_counter() - started
        )
        ctx: dict[str, Any] = {"event_id": message.event_id, "event_type": message.event_type,
               "sink": sink_name, "delivery_id": delivery_id}
        if error is None:
            self.breaker.success(sink_name)
            if self._finish(delivery_id, status=DELIVERED, delivered_at=_now(),
                            last_error=None, last_status_code=None):
                metrics.NOTIFICATION_DELIVERIES.labels(sink_name, "delivered").inc()
                log_event(logger, "notification delivered", logging.INFO, **ctx)
            return
        self.breaker.failure(sink_name)
        status_code = error.context.get("status_code")
        detail = {
            "last_error": f"{error.code}: {error.message}"[:_ERROR_TEXT_LIMIT],
            "last_status_code": status_code if isinstance(status_code, int) else None,
        }
        exhausted = message.attempt >= self._settings.notification_max_attempts
        if error.retryable and not exhausted:
            due = _now() + timedelta(seconds=self._backoff_s(message.attempt))
            if self._finish(delivery_id, next_attempt_at=due, **detail):
                metrics.NOTIFICATION_DELIVERIES.labels(sink_name, "retry").inc()
                log_event(logger, "notification delivery failed, will retry", logging.WARNING,
                          error=detail["last_error"], **ctx)
            return
        if self._finish(delivery_id, status=DEAD, dead_at=_now(), **detail):
            metrics.NOTIFICATION_DELIVERIES.labels(sink_name, "dead").inc()
            log_event(logger, "notification dead-lettered", logging.ERROR,
                      error=detail["last_error"], **ctx)

    # ---- loop ----------------------------------------------------------------------------
    def run_once(self) -> int:
        """Claim and deliver one batch; the number of deliveries claimed."""
        claimed = self._claim()
        for delivery_id in claimed:
            self._deliver(delivery_id)
        self.update_backlog()
        return len(claimed)

    def drain(self, max_rounds: int) -> int:
        """Poll until nothing is due (or ``max_rounds``); the deliveries processed."""
        total = 0
        for _ in range(max_rounds):
            n = self.run_once()
            total += n
            if n == 0:
                break
        return total

    def update_backlog(self) -> None:
        with session_scope(self._sessions) as session:
            counts = dict(
                session.execute(
                    select(NotificationDelivery.status, func.count())
                    .where(NotificationDelivery.status.in_((PENDING, DEAD)))
                    .group_by(NotificationDelivery.status)
                ).tuples().all()
            )
        for status in (PENDING, DEAD):
            metrics.NOTIFICATION_BACKLOG.labels(status).set(counts.get(status, 0))

    def run_forever(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. A failed poll (database down) is logged and retried
        after the idle interval; it never ends the loop."""
        interval = self._settings.notification_dispatch_interval_s
        while not stop.is_set():
            try:
                busy = self.run_once() >= self._settings.notification_dispatch_batch
            except Exception:  # the loop outlives a database outage; each failure is logged
                logger.exception("notification dispatch poll failed")
                busy = False
            if not busy:
                stop.wait(interval)


class DispatcherThread:
    """The dispatcher on a daemon thread, for the API process (NOTIFICATION_DISPATCH_ENABLED)."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start polling (again, after a stop: an app's lifespan may run more than once)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self.dispatcher.run_forever, args=(self._stop,),
            name="notification-dispatcher", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
