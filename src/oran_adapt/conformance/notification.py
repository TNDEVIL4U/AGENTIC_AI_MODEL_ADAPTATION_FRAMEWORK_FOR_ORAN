"""Conformance suite for ``NotificationPort`` adapters (notification sinks).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
context's ``received()`` returns every payload the sink's receiving end has seen so far (the raw
bytes of an HTTP body, a broker record, an email...), in order::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_sink(check):
        CHECKS[check](MySink(...), Context(received=my_receiver.payloads))

``FAILURE_CHECKS`` need ``Context.inject_failure(retryable)``, which makes the receiving end
refuse the next message - temporarily (``True``: down, overloaded, 5xx) or for good (``False``:
bad credentials, bad request). The dispatcher retries only what the adapter marks retryable, so
a sink that gets this wrong either loses events or retries a hopeless request until it dies.
docs/adapters/notification.md explains each rule.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import NotificationDeliveryError
from oran_adapt.notifications.events import body_of
from oran_adapt.notifications.signing import sign
from oran_adapt.ports import NotificationPort, OutboundMessage

_names = itertools.count(1)


@dataclass
class Context:
    received: Callable[[], list[bytes]]
    """Every payload the receiving end has seen, oldest first."""
    inject_failure: Callable[[bool], None] | None = None
    """``inject_failure(retryable)``: the receiving end refuses the next message."""
    signing_key: bytes = b"conformance-signing-key-0123456789abcdef"
    prefix: str = "conformance"
    _run: int = field(default_factory=lambda: next(_names), repr=False)

    def message(self, label: str, event_type: str = "job.completed") -> OutboundMessage:
        """A signed message as the dispatcher would build it; the subject is unique per call."""
        event_id = f"{self.prefix}{self._run}{label}".replace("-", "")[:64]
        subject = f"{self.prefix}-{label}-{self._run}"
        envelope = {
            "specversion": "1.0",
            "id": event_id,
            "source": "oran-adapt",
            "type": f"oran.adapt.{event_type}",
            "subject": subject,
            "time": datetime.now(UTC).isoformat(),
            "datacontenttype": "application/json",
            "data": {"job_id": subject, "model_id": "conformance-model",
                     "from_status": "DEPLOYING", "to_status": "COMPLETED",
                     "message": "conformance check"},
        }
        body = body_of(envelope)
        return OutboundMessage(
            event_id=event_id, event_type=event_type, subject=subject, envelope=envelope,
            body=body, headers=sign(event_id, body, [self.signing_key]),
        )


def _mentions(payload: bytes, subject: str) -> bool:
    return subject.encode("utf-8") in payload


def check_protocol(port: NotificationPort, ctx: Context) -> None:
    expect(isinstance(port, NotificationPort), "does not implement NotificationPort")


def check_ping(port: NotificationPort, ctx: Context) -> None:
    before = len(ctx.received())
    port.ping()
    expect(len(ctx.received()) == before, "ping delivered a message")


def check_send_delivers(port: NotificationPort, ctx: Context) -> None:
    message = ctx.message("send")
    before = len(ctx.received())
    port.send(message)
    seen = ctx.received()[before:]
    expect(len(seen) == 1, f"one send reached the receiver {len(seen)} times")
    expect(_mentions(seen[0], message.subject),
           "the delivered payload does not name the job (the event subject)")


def check_resend_accepted(port: NotificationPort, ctx: Context) -> None:
    """Delivery is at-least-once: after a crash the same event is sent again, and the sink must
    accept it (receivers deduplicate on the event id)."""
    message = ctx.message("resend")
    before = len(ctx.received())
    port.send(message)
    port.send(message)
    expect(len(ctx.received()) - before == 2, "a resent event was not delivered again")


def check_event_types(port: NotificationPort, ctx: Context) -> None:
    for event_type in ("job.received", "job.failed", "job.rolled_back"):
        message = ctx.message(event_type.replace(".", ""), event_type)
        before = len(ctx.received())
        port.send(message)
        expect(len(ctx.received()) - before == 1, f"{event_type} was not delivered")


def _refused(port: NotificationPort, ctx: Context, retryable: bool) -> NotificationDeliveryError:
    if ctx.inject_failure is None:
        raise ConformanceFailure("failure checks need Context.inject_failure")
    ctx.inject_failure(retryable)
    try:
        port.send(ctx.message("refused" if not retryable else "busy"))
    except NotificationDeliveryError as exc:
        return exc
    except Exception as exc:
        raise ConformanceFailure(
            f"a refused send raised {type(exc).__name__}, not NotificationDeliveryError"
        ) from exc
    raise ConformanceFailure("a message the receiver refused was reported as delivered")


def check_temporary_failure_retryable(port: NotificationPort, ctx: Context) -> None:
    exc = _refused(port, ctx, retryable=True)
    expect(exc.retryable, f"a temporary refusal was marked permanent: {exc}")


def check_permanent_failure_not_retryable(port: NotificationPort, ctx: Context) -> None:
    exc = _refused(port, ctx, retryable=False)
    expect(not exc.retryable, f"a permanent refusal was marked retryable: {exc}")


def check_recovers_after_failure(port: NotificationPort, ctx: Context) -> None:
    _refused(port, ctx, retryable=True)
    check_send_delivers(port, ctx)


CHECKS: dict[str, Callable[[NotificationPort, Context], None]] = {
    "protocol": check_protocol,
    "ping": check_ping,
    "send_delivers": check_send_delivers,
    "resend_accepted": check_resend_accepted,
    "event_types": check_event_types,
}

FAILURE_CHECKS: dict[str, Callable[[NotificationPort, Context], None]] = {
    "temporary_failure_retryable": check_temporary_failure_retryable,
    "permanent_failure_not_retryable": check_permanent_failure_not_retryable,
    "recovers_after_failure": check_recovers_after_failure,
}


def run(port: NotificationPort, ctx: Context) -> list[str]:
    """Run every check (and the failure checks when ``ctx.inject_failure`` is set) in order;
    returns their names. Stops at the first failure."""
    checks = dict(CHECKS)
    if ctx.inject_failure is not None:
        checks.update(FAILURE_CHECKS)
    for check in checks.values():
        check(port, ctx)
    return list(checks)


__all__ = ["CHECKS", "FAILURE_CHECKS", "ConformanceFailure", "Context", "run"]
