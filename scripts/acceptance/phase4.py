"""Hardening Phase 4 acceptance: outbound notifications, checked end to end.

Run by scripts/verify.sh 4 after lint, the import-boundary test and the scoped tests.

1. The fake-receiver suite: a signed webhook delivered once; 500s retried with backoff and,
   when they never stop, dead-lettered; a timed-out attempt retried; a sink down until its
   deliveries die, then up again and redriven over the API; a receiver rejecting a bad
   signature (dead at once, not retried); and the API process killed while its POST is in
   flight, with the event resent after the lease under the same id (no event lost).
2. Every job state transition writes one notification event, in the transition's
   transaction; a refused transition writes none.
3. Every installed notification adapter passes the notification conformance suite against a
   local receiving end (HTTP receiver, NATS server, SMTP/Kafka/AWS doubles). The doubles are
   not the real systems: unverified against them.
4. Vendor SDKs stay inside their adapters, every notification adapter is documented in
   docs/adapters/notification.md, and the receiver template verifies what the dispatcher signs.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # notification doubles, test helpers


def fake_receiver_suite(tmp: Path) -> str:
    import test_phase4_notifications as t

    lines = []
    for name, scenario in t.SCENARIOS.items():
        root = tmp / name
        root.mkdir()
        lines.append(f"{name}: {scenario(root)}")
    return "\n      ".join(lines)


def an_event_for_every_transition(tmp: Path) -> str:
    import test_phase4_notifications as t

    t.test_every_transition_writes_an_event_and_a_refused_one_writes_none(tmp)
    return "RECEIVED -> ... -> COMPLETED: one event per transition, none for a refused one"


def conformance_everywhere(tmp: Path) -> str:
    import test_phase4_notifications as t

    from oran_adapt import plugins
    from oran_adapt.conformance.notification import CHECKS, FAILURE_CHECKS, Context, run

    installed = set(plugins.adapters("notification"))
    assert installed == set(t.HARNESSES), f"adapters without a harness: {installed ^ set(t.HARNESSES)}"
    full = 0
    for name, build in t.HARNESSES.items():
        h = build()
        try:
            ran = run(h.port, Context(received=h.received, inject_failure=h.inject_failure,
                                      prefix=name))
        finally:
            h.close()
        expected = [*CHECKS, *FAILURE_CHECKS] if h.inject_failure else list(CHECKS)
        assert ran == expected, f"{name}: ran {ran}"
        full += h.inject_failure is not None
    return (f"{len(t.HARNESSES)} adapters ({full} with the failure checks; log cannot fail) "
            "against local doubles; unverified against the real systems")


def boundaries_docs_and_template(tmp: Path) -> str:
    import importlib.util

    from test_import_boundary import violations

    from oran_adapt import plugins
    from oran_adapt.notifications.signing import sign

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    path = ROOT / "docs" / "adapters" / "notification.md"
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    guide = path.read_text(encoding="utf-8")
    undocumented = [a for a in plugins.adapters("notification") if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/notification.md: {undocumented}"

    template = ROOT / "templates" / "notification-receiver" / "receiver.py"
    spec = importlib.util.spec_from_file_location("notification_receiver", template)
    assert spec is not None and spec.loader is not None, f"{template} is missing"
    receiver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receiver)
    key = bytes(range(32))
    body = b'{"id":"e1"}'
    headers = sign("e1", body, [key])
    assert receiver.verify(body, headers, [key], tolerance_s=300) == "e1"
    try:
        receiver.verify(body + b" ", headers, [key], tolerance_s=300)
    except receiver.InvalidSignature:
        pass
    else:
        raise AssertionError("the receiver template accepted a tampered body")
    return "import boundary clean; all adapters documented; template verifies real signatures"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("fake-receiver suite", fake_receiver_suite),
    ("an event for every state transition", an_event_for_every_transition),
    ("conformance suite green for every notification adapter", conformance_everywhere),
    ("boundaries, documentation and the receiver template", boundaries_docs_and_template),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase4-", ignore_cleanup_errors=True) as tmp:
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}")
            else:
                print(f"PASS  {name}\n      {detail}")
    print(f"\nphase 4 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
