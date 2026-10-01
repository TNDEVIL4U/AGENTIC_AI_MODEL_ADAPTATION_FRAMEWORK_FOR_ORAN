"""Hardening Phase 4 (finding 3): outbound notifications.

Every job state transition writes a CloudEvents event to a durable outbox in the transition's
own transaction; a dispatcher delivers each event to every configured sink with HMAC signing
(key rotation), exponential backoff, a per-sink circuit breaker and a dead-letter state;
GET /api/v1/deliveries shows what happened and redrive re-queues the dead.

The gate scenarios run against local receiving ends (tests/unit/notification_doubles.py):
normal delivery, 500s, a timeout, a sink down then up and redriven, a receiver rejecting a bad
signature, and the API process killed mid-delivery with no event lost. Every sink adapter runs
the conformance suite (oran_adapt.conformance.notification). The scenario functions are
module-level so scripts/acceptance/phase4.py runs the same code.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from notification_doubles import (
    FakeAws,
    FakeClientError,
    FakeKafkaException,
    FakeProducer,
    LogCapture,
    NatsServer,
    Receiver,
    SmtpServer,
    free_port,
    pubsub_decode,
)
from sqlalchemy import inspect, select

from oran_adapt import plugins
from oran_adapt.adapters.notify import LogSink, PagerDutySink, SlackSink, WebhookSink
from oran_adapt.adapters.notify_brokers import KafkaSink, NatsSink, PubSubSink, SnsSink, SqsSink
from oran_adapt.adapters.notify_email import EmailSink
from oran_adapt.api.app import create_app
from oran_adapt.bootstrap import build_notifiers
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance.notification import CHECKS, FAILURE_CHECKS, Context, run
from oran_adapt.core.config import Settings, selected_adapters
from oran_adapt.core.enums import AuditAction, JobStatus
from oran_adapt.core.errors import (
    ConfigurationError,
    InvalidTransitionError,
    NotificationDeliveryError,
    SignatureVerificationError,
)
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.migrate import downgrade_to_base, upgrade_to_head
from oran_adapt.db.models import (
    AdaptationEvent,
    AdaptationJob,
    AuditLog,
    NotificationDelivery,
    NotificationEvent,
)
from oran_adapt.notifications.dispatcher import CircuitBreaker, Dispatcher
from oran_adapt.notifications.events import DEAD, DELIVERED, PENDING, body_of, record_event
from oran_adapt.notifications.signing import (
    ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    parse_keys,
    sign,
    verify,
)
from oran_adapt.orchestrator.jobs import _transition

KEY = bytes(range(32))
NEW_KEY = bytes(range(32, 64))
OTHER_KEY = bytes(range(64, 96))


def spec(*keys: bytes) -> str:
    return ",".join("whsec_" + base64.b64encode(k).decode() for k in keys)


# Fast, deterministic delivery for tests: tiny backoff, no jitter, a breaker that stays shut.
FAST = {
    "notification_backoff_initial_s": 0.05,
    "notification_backoff_max_s": 0.2,
    "notification_backoff_jitter": 0.0,
    "notification_max_attempts": 3,
    "notification_timeout_s": 2.0,
    "notification_breaker_failures": 100,
    "notification_dispatch_interval_s": 0.1,
}


def make_settings(tmp: Path, **overrides: Any) -> Settings:
    """Settings on a fresh, migrated SQLite database under ``tmp`` (never the .env file)."""
    tmp.mkdir(parents=True, exist_ok=True)
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{(tmp / 'app.db').as_posix()}",
        "mlflow_tracking_uri": f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        "artifact_workdir": str(tmp / "work"),
        "log_json": False,
        "auth_enabled": False,
        "notification_dispatch_enabled": False,
        **overrides,
    }
    settings = Settings(_env_file=None, **values)
    upgrade_to_head(settings.database_url)
    return settings


def webhook_settings(tmp: Path, url: str, **overrides: Any) -> Settings:
    return make_settings(
        tmp, notification_backend="webhook", notification_webhook_url=url,
        notification_signing_keys=spec(KEY), **{**FAST, **overrides},
    )


def sessions(settings: Settings):
    return make_session_factory(create_db_engine(settings.database_url))


def emit(factory, settings: Settings, subject: str = "job-1",
         event_type: str = "job.completed") -> str:
    with session_scope(factory) as session:
        return record_event(session, settings, event_type=event_type, subject=subject,
                            data={"job_id": subject, "to_status": "COMPLETED"},
                            model_id="cell-a")


def rows(factory) -> list[dict[str, Any]]:
    with session_scope(factory) as session:
        return [
            {"id": d.id, "event_id": d.event_id, "sink": d.sink, "status": d.status,
             "attempts": d.attempts, "code": d.last_status_code, "error": d.last_error,
             "redrives": d.redrive_count}
            for d in session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
        ]


def dispatcher(factory, settings: Settings, **kw: Any) -> Dispatcher:
    return Dispatcher(factory, build_notifiers(settings), settings, rng=lambda: 0.5, **kw)


def settle(d: Dispatcher, factory, until: Callable[[list[dict[str, Any]]], bool],
           timeout_s: float = 20.0) -> list[dict[str, Any]]:
    """Poll the dispatcher until ``until(rows)`` holds."""
    deadline = time.monotonic() + timeout_s
    while True:
        d.run_once()
        current = rows(factory)
        if until(current):
            return current
        if time.monotonic() > deadline:
            raise AssertionError(f"deliveries never settled: {current}")
        time.sleep(0.05)


def only(status: str) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda rs: bool(rs) and all(r["status"] == status for r in rs)


# ---- gate scenarios (shared with scripts/acceptance/phase4.py) ----------------------------
def scenario_normal_delivery(tmp: Path) -> str:
    receiver = Receiver(keys=[KEY]).start()
    try:
        settings = webhook_settings(tmp, receiver.url)
        factory = sessions(settings)
        event_id = emit(factory, settings)
        [row] = settle(dispatcher(factory, settings), factory, only(DELIVERED))
        assert row["attempts"] == 1, row
        [(headers, body)] = receiver.accepted
        assert headers[ID_HEADER] == event_id
        assert headers["Content-Type"] == "application/cloudevents+json"
        envelope = json.loads(body)
        assert envelope["specversion"] == "1.0" and envelope["id"] == event_id
        assert envelope["type"] == "oran.adapt.job.completed" and envelope["subject"] == "job-1"
        assert verify(body, headers, [KEY], tolerance_s=60) == event_id
    finally:
        receiver.stop()
    return "delivered once, signed, CloudEvents envelope"


def scenario_500_then_success(tmp: Path) -> str:
    receiver = Receiver(keys=[KEY]).start()
    receiver.statuses = [500, 500]
    try:
        settings = webhook_settings(tmp, receiver.url)
        factory = sessions(settings)
        emit(factory, settings)
        [row] = settle(dispatcher(factory, settings), factory, only(DELIVERED))
        assert row["attempts"] == 3 and receiver.posts == 3, (row, receiver.posts)
        assert len(receiver.accepted) == 1
    finally:
        receiver.stop()
    return "two 500s retried with backoff, delivered on attempt 3"


def scenario_500s_dead_letter(tmp: Path) -> str:
    receiver = Receiver(keys=[KEY]).start()
    receiver.statuses = [500] * 10
    try:
        settings = webhook_settings(tmp, receiver.url)
        factory = sessions(settings)
        emit(factory, settings)
        [row] = settle(dispatcher(factory, settings), factory, only(DEAD))
        assert row["attempts"] == 3 and row["code"] == 500, row
        assert "HTTP 500" in row["error"]
    finally:
        receiver.stop()
    return "500 on every attempt: DEAD after NOTIFICATION_MAX_ATTEMPTS"


def scenario_timeout(tmp: Path) -> str:
    receiver = Receiver(keys=[KEY]).start()
    receiver.delay_s = 2.0
    try:
        settings = webhook_settings(tmp, receiver.url, notification_timeout_s=0.3)
        factory = sessions(settings)
        emit(factory, settings)
        d = dispatcher(factory, settings)
        d.run_once()
        [row] = rows(factory)
        assert row["status"] == PENDING and "timed out" in row["error"], row
        receiver.delay_s = 0.0
        [row] = settle(d, factory, only(DELIVERED))
        assert row["attempts"] == 2, row
    finally:
        receiver.stop()
    return "a timed-out attempt is retried and then delivered"


def scenario_down_then_up_redrive(tmp: Path) -> str:
    port = free_port()
    settings = webhook_settings(tmp, f"http://127.0.0.1:{port}/hook", notification_timeout_s=1.0)
    factory = sessions(settings)
    event_id = emit(factory, settings)
    d = dispatcher(factory, settings)
    [row] = settle(d, factory, only(DEAD))
    # Refused (Linux) or timed out (Windows retries a refused local connect): both are "down".
    assert row["attempts"] == 3 and row["code"] is None, row
    assert "unreachable" in row["error"] or "timed out" in row["error"], row

    receiver = Receiver(port=port, keys=[KEY]).start()
    try:
        with TestClient(create_app(settings)) as api:
            dead = api.get("/api/v1/deliveries", params={"status": DEAD}).json()
            assert dead["total"] == 1 and dead["items"][0]["event_id"] == event_id, dead
            r = api.post(f"/api/v1/deliveries/{row['id']}/redrive")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == PENDING and r.json()["redrive_count"] == 1
            again = api.post(f"/api/v1/deliveries/{row['id']}/redrive")
            assert again.status_code == 409 and again.json()["code"] == "CONFLICT"
        [row] = settle(d, factory, only(DELIVERED))
        assert [h[ID_HEADER] for h, _ in receiver.accepted] == [event_id]
        with session_scope(factory) as session:
            audit = session.scalars(select(AuditLog).where(
                AuditLog.action == AuditAction.NOTIFICATION_REDRIVEN)).all()
            assert len(audit) == 1 and audit[0].actor == "anonymous"
    finally:
        receiver.stop()
    return "dead while the sink was down; redriven over the API and delivered, same event id"


def scenario_bad_signature(tmp: Path) -> str:
    receiver = Receiver(keys=[OTHER_KEY]).start()
    try:
        settings = webhook_settings(tmp, receiver.url)
        factory = sessions(settings)
        emit(factory, settings)
        [row] = settle(dispatcher(factory, settings), factory, only(DEAD))
        assert row["attempts"] == 1 and row["code"] == 401, row
        assert receiver.rejected_signatures == 1 and not receiver.accepted
    finally:
        receiver.stop()
    return "the receiver rejected the signature (401): dead at once, not retried"


_API_PROCESS = """
import json, os, uvicorn
from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
settings = Settings(_env_file=None, **json.loads(os.environ["ORAN_TEST_SETTINGS"]))
uvicorn.run(create_app(settings), host="127.0.0.1", port=int(os.environ["ORAN_TEST_PORT"]),
            log_level="warning")
"""


def scenario_api_killed_mid_delivery(tmp: Path) -> str:
    """The API process (dispatcher thread on) is killed while its POST is in flight. The event
    was committed with the transition, so it is not lost: once the lease runs out another
    dispatcher sends it again, under the same event id."""
    receiver = Receiver(keys=[KEY]).start()
    receiver.gate = threading.Event()  # hold the first POST open
    lease_s = 2.0
    values = {
        "notification_backend": "webhook", "notification_webhook_url": receiver.url,
        "notification_signing_keys": spec(KEY), "notification_lease_s": lease_s,
        **FAST, "notification_timeout_s": 60.0,
    }
    settings = make_settings(tmp, **values)
    factory = sessions(settings)
    event_id = emit(factory, settings)
    env = {
        **os.environ,
        "ORAN_TEST_PORT": str(free_port()),
        "ORAN_TEST_SETTINGS": json.dumps({
            "database_url": settings.database_url,
            "mlflow_tracking_uri": settings.mlflow_tracking_uri,
            "artifact_workdir": settings.artifact_workdir,
            "log_json": False, "auth_enabled": False,
            "notification_dispatch_enabled": True, **values,
        }),
    }
    proc = subprocess.Popen([sys.executable, "-c", _API_PROCESS], env=env, cwd=str(tmp),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 90
        while receiver.posts == 0:
            assert proc.poll() is None, "the API process exited before delivering"
            assert time.monotonic() < deadline, "the API never started delivering"
            time.sleep(0.1)
        [row] = rows(factory)
        assert row["status"] == PENDING and row["attempts"] == 1, row
    finally:
        proc.kill()  # the process this test started, and only it
        proc.wait(timeout=30)
    receiver.gate = None
    try:
        [row] = rows(factory)
        assert row["status"] == PENDING, row  # the killed process recorded nothing
        d = dispatcher(factory, settings)
        assert d.run_once() == 0  # still leased to the dead process
        [row] = settle(d, factory, only(DELIVERED), timeout_s=lease_s + 20)
        assert row["attempts"] == 2, row
        ids = {h[ID_HEADER] for h, _ in receiver.accepted}
        assert ids == {event_id}, ids
    finally:
        receiver.release()
        receiver.stop()
    return "API killed mid-delivery: resent after the lease under the same event id"


SCENARIOS: dict[str, Callable[[Path], str]] = {
    "normal_delivery": scenario_normal_delivery,
    "500_then_success": scenario_500_then_success,
    "500s_dead_letter": scenario_500s_dead_letter,
    "timeout": scenario_timeout,
    "down_then_up_redrive": scenario_down_then_up_redrive,
    "bad_signature": scenario_bad_signature,
    "api_killed_mid_delivery": scenario_api_killed_mid_delivery,
}


@pytest.mark.parametrize("name", [n for n in SCENARIOS if n != "api_killed_mid_delivery"])
def test_gate_scenario(name: str, tmp_path: Path) -> None:
    SCENARIOS[name](tmp_path)


@pytest.mark.heavy  # starts a real API process; the phase gate runs it in the acceptance script
def test_gate_scenario_api_killed_mid_delivery(tmp_path: Path) -> None:
    scenario_api_killed_mid_delivery(tmp_path)


# ---- events on every transition ------------------------------------------------------------
def assert_every_transition_notified(factory) -> None:
    """Each AdaptationEvent (a recorded job transition) has its notification event."""
    with session_scope(factory) as session:
        transitions = [(e.job_id, e.to_status) for e in
                       session.scalars(select(AdaptationEvent).order_by(AdaptationEvent.id))]
        events = [(e.subject, e.envelope["data"]["to_status"]) for e in
                  session.scalars(select(NotificationEvent).order_by(NotificationEvent.id))]
    assert transitions and events == transitions, (transitions, events)


@pytest.mark.smoke
def test_every_transition_writes_an_event_and_a_refused_one_writes_none(tmp_path) -> None:
    settings = make_settings(tmp_path)
    factory = sessions(settings)
    with session_scope(factory) as session:
        session.add(AdaptationJob(job_id="j1", idempotency_key="k1", model_id="cell-a",
                                  status=JobStatus.RECEIVED, event={"model_id": "cell-a"}))
    path = [JobStatus.VALIDATING, JobStatus.DATA_PREPARING, JobStatus.EVALUATING_VERSIONS,
            JobStatus.REUSE_DECISION, JobStatus.PROMOTING, JobStatus.COMPLETED]
    for status in path:
        _transition(factory, "j1", settings=settings, to_status=status, message=f"-> {status}")
    with pytest.raises(InvalidTransitionError):
        _transition(factory, "j1", settings=settings, to_status=JobStatus.ADAPTING)
    assert_every_transition_notified(factory)
    with session_scope(factory) as session:
        types = [e.event_type for e in
                 session.scalars(select(NotificationEvent).order_by(NotificationEvent.id))]
        last = session.scalars(select(NotificationEvent).order_by(NotificationEvent.id.desc())
                               ).first()
        assert last is not None
        assert last.model_id == "cell-a" and last.envelope["data"]["from_status"] == "PROMOTING"
    assert types == [f"job.{s.value.lower()}" for s in path]


@pytest.mark.smoke
def test_sinks_receive_only_their_configured_event_types(tmp_path) -> None:
    settings = make_settings(tmp_path, notification_backend="log,pagerduty",
                             notification_pagerduty_routing_key="rk")
    factory = sessions(settings)
    emit(factory, settings, "ok-job", "job.completed")
    emit(factory, settings, "bad-job", "job.failed")
    by_event: dict[str, list[str]] = {}
    with session_scope(factory) as session:
        for d, e in session.execute(select(NotificationDelivery, NotificationEvent).join(
                NotificationEvent, NotificationEvent.event_id == NotificationDelivery.event_id)):
            by_event.setdefault(e.event_type, []).append(d.sink)
    assert by_event == {"job.completed": ["log"], "job.failed": ["log", "pagerduty"]}


@pytest.mark.smoke
def test_notification_backend_none_records_events_without_deliveries(tmp_path) -> None:
    settings = make_settings(tmp_path, notification_backend="none")
    assert selected_adapters(settings, "notification_backend") == []
    assert build_notifiers(settings) == {}
    factory = sessions(settings)
    emit(factory, settings)
    assert rows(factory) == []


# ---- signing -------------------------------------------------------------------------------
@pytest.mark.smoke
def test_signature_verification_rules() -> None:
    body = b'{"id":"e1"}'
    headers = sign("e1", body, [KEY], timestamp=1_000)
    assert verify(body, headers, [KEY], tolerance_s=60, now=1_030) == "e1"
    # Header names are case-insensitive.
    upper = {k.upper(): v for k, v in headers.items()}
    assert verify(body, upper, [KEY], tolerance_s=60, now=1_000) == "e1"
    bad = [
        (b'{"id":"e2"}', headers, [KEY], 1_000),  # tampered body
        (body, headers, [OTHER_KEY], 1_000),  # wrong key
        (body, headers, [KEY], 1_061),  # stale timestamp
        (body, {**headers, ID_HEADER: "e9"}, [KEY], 1_000),  # id swapped
        (body, {ID_HEADER: "e1", TIMESTAMP_HEADER: "1000"}, [KEY], 1_000),  # unsigned
    ]
    for b, h, keys, now in bad:
        with pytest.raises(SignatureVerificationError):
            verify(b, h, keys, tolerance_s=60, now=now)


@pytest.mark.smoke
def test_key_rotation_signs_with_every_key() -> None:
    body = b"{}"
    headers = sign("e1", body, [KEY, NEW_KEY], timestamp=5)
    assert len(headers[SIGNATURE_HEADER].split()) == 2
    # A receiver on the old key and one already on the new key both accept.
    assert verify(body, headers, [KEY], tolerance_s=10, now=5) == "e1"
    assert verify(body, headers, [NEW_KEY], tolerance_s=10, now=5) == "e1"
    assert SIGNATURE_HEADER not in sign("e1", body, [])


@pytest.mark.smoke
def test_signing_keys_parse_and_short_keys_are_refused(tmp_path) -> None:
    assert parse_keys(spec(KEY, NEW_KEY), 32) == [KEY, NEW_KEY]
    assert parse_keys("x" * 40, 32) == [b"x" * 40]
    with pytest.raises(ConfigurationError) as ei:
        parse_keys("whsec_" + base64.b64encode(b"short").decode(), 32)
    assert ei.value.context["key"] == "NOTIFICATION_SIGNING_KEYS"
    with pytest.raises(ConfigurationError):
        make_settings(tmp_path, notification_signing_keys="too-short")


# ---- dispatcher internals ------------------------------------------------------------------
@pytest.mark.smoke
def test_circuit_breaker_opens_then_trials_one_delivery() -> None:
    now = [0.0]
    breaker = CircuitBreaker(2, 30.0, lambda: now[0])
    breaker.failure("s")
    assert breaker.allow("s")
    breaker.failure("s")
    assert breaker.is_open("s") and not breaker.allow("s")
    now[0] = 31.0
    assert breaker.allow("s")  # the one trial
    assert not breaker.allow("s")  # no second trial while the first is out
    breaker.success("s")
    assert not breaker.is_open("s") and breaker.allow("s")


def test_open_circuit_holds_deliveries_without_spending_attempts(tmp_path) -> None:
    receiver = Receiver(keys=[KEY]).start()
    receiver.statuses = [503, 503]
    now = [0.0]
    try:
        settings = webhook_settings(tmp_path, receiver.url, notification_breaker_failures=2,
                                    notification_breaker_reset_s=30.0,
                                    notification_max_attempts=5)
        factory = sessions(settings)
        for n in range(3):
            emit(factory, settings, f"job-{n}")
        d = dispatcher(factory, settings, clock=lambda: now[0])
        d.run_once()
        attempts = [r["attempts"] for r in rows(factory)]
        assert attempts == [1, 1, 0], attempts  # the third was held back, not attempted
        assert receiver.posts == 2
        assert d.run_once() == 0  # open: nothing is even claimed
        with session_scope(factory) as session:
            held = session.get(NotificationDelivery, 3)
            assert held is not None and held.leased_by is None
            wait_s = (held.next_attempt_at.replace(tzinfo=None) - datetime.now(UTC)
                      .replace(tzinfo=None)).total_seconds()
            assert 20 < wait_s <= 30, wait_s  # handed back until the breaker's reset
            held.next_attempt_at = datetime.now(UTC)
        now[0] = 31.0
        time.sleep(0.25)  # past the backoff of the first two
        settle(d, factory, only(DELIVERED))
        assert len(receiver.accepted) == 3
        assert [r["attempts"] for r in rows(factory)] == [2, 2, 1]
    finally:
        receiver.stop()


def test_a_second_dispatcher_cannot_record_over_a_reclaimed_delivery(tmp_path) -> None:
    receiver = Receiver(keys=[KEY]).start()
    try:
        settings = webhook_settings(tmp_path, receiver.url, notification_lease_s=0.2)
        factory = sessions(settings)
        emit(factory, settings)
        first = dispatcher(factory, settings, worker_id="first")
        [claimed] = first._claim()
        time.sleep(0.3)  # the lease runs out: the delivery is claimable again
        second = dispatcher(factory, settings, worker_id="second")
        assert second.run_once() == 1
        assert not first._finish(claimed, status=DEAD)  # the stale worker changes nothing
        [row] = rows(factory)
        assert row["status"] == DELIVERED and row["attempts"] == 2, row
    finally:
        receiver.stop()


def test_api_lifespan_runs_the_dispatcher(tmp_path) -> None:
    receiver = Receiver(keys=[KEY]).start()
    try:
        settings = webhook_settings(tmp_path, receiver.url, notification_dispatch_enabled=True)
        factory = sessions(settings)
        emit(factory, settings)
        with TestClient(create_app(settings)):
            deadline = time.monotonic() + 20
            while not receiver.accepted:
                assert time.monotonic() < deadline, rows(factory)
                time.sleep(0.05)
        assert rows(factory)[0]["status"] in (PENDING, DELIVERED)
    finally:
        receiver.stop()


# ---- API -----------------------------------------------------------------------------------
def test_deliveries_api_filters_pages_and_errors(tmp_path) -> None:
    settings = make_settings(tmp_path, notification_backend="log,pagerduty",
                             notification_pagerduty_routing_key="rk")
    factory = sessions(settings)
    emit(factory, settings, "a", "job.completed")
    emit(factory, settings, "b", "job.failed")
    with TestClient(create_app(settings)) as api:
        everything = api.get("/api/v1/deliveries").json()
        assert everything["total"] == 3 and len(everything["items"]) == 3
        assert everything["items"][0]["id"] > everything["items"][-1]["id"]  # newest first
        page = api.get("/api/v1/deliveries", params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 3 and len(page["items"]) == 1
        for params, total in (({"sink": "pagerduty"}, 1), ({"event_type": "job.failed"}, 2),
                              ({"subject": "a"}, 1), ({"status": PENDING}, 3),
                              ({"status": DEAD}, 0)):
            assert api.get("/api/v1/deliveries", params=params).json()["total"] == total, params
        assert api.get("/api/v1/deliveries", params={"status": "BOGUS"}).status_code == 422
        one = api.get(f"/api/v1/deliveries/{everything['items'][0]['id']}").json()
        assert one["envelope"]["subject"] == "b" and "envelope" not in everything["items"][0]
        missing = api.get("/api/v1/deliveries/999999")
        assert missing.status_code == 404 and missing.json()["code"] == "DELIVERY_NOT_FOUND"
        pending = api.post(f"/api/v1/deliveries/{one['id']}/redrive")
        assert pending.status_code == 409
        assert api.post("/api/v1/deliveries/999999/redrive").status_code == 404
        # Bulk redrive of the dead (none yet) and after two die.
        assert api.post("/api/v1/deliveries/redrive", json={}).json() == {"redriven": 0,
                                                                          "ids": []}
        with session_scope(factory) as session:
            for d in session.scalars(select(NotificationDelivery)):
                d.status = DEAD if d.sink == "log" else d.status
        bulk = api.post("/api/v1/deliveries/redrive", json={"sink": "log", "limit": 1}).json()
        assert bulk["redriven"] == 1
        rest = api.post("/api/v1/deliveries/redrive", json={"sink": "log"}).json()
        assert rest["redriven"] == 1
    assert all(r["status"] == PENDING for r in rows(factory))


# ---- configuration ------------------------------------------------------------------------
@pytest.mark.smoke
def test_config_names_the_missing_key_of_each_selected_sink(tmp_path) -> None:
    with pytest.raises(ConfigurationError) as ei:
        make_settings(tmp_path, notification_backend="webhook,slack",
                      notification_webhook_url="http://127.0.0.1:1/x")
    assert "NOTIFICATION_SLACK_WEBHOOK_URL" in str(ei.value.to_dict())
    with pytest.raises(ConfigurationError) as ei:
        make_settings(tmp_path, notification_backend="log,carrier-pigeon")
    assert "carrier-pigeon" in str(ei.value.to_dict())


@pytest.mark.smoke
def test_production_webhook_requires_signing_keys(tmp_path) -> None:
    with pytest.raises(ConfigurationError) as ei:
        Settings(_env_file=None, environment="production", notification_backend="webhook",
                 notification_webhook_url="https://hooks.example.org/x")
    assert "NOTIFICATION_SIGNING_KEYS" in str(ei.value.to_dict())


@pytest.mark.smoke
def test_capabilities_list_every_selected_sink(tmp_path) -> None:
    settings = make_settings(tmp_path, notification_backend="log,slack",
                             notification_slack_webhook_url="http://127.0.0.1:1/x")
    with TestClient(create_app(settings)) as api:
        ports = api.get("/api/v1/capabilities").json()["ports"]
    assert ports["notification"]["selected"] == ["log", "slack"]
    assert ports["registry"]["selected"] == settings.registry_backend


@pytest.mark.smoke
def test_migration_0007_up_and_down(tmp_path) -> None:
    settings = make_settings(tmp_path)
    tables = set(inspect(create_db_engine(settings.database_url)).get_table_names())
    assert {"notification_event", "notification_delivery"} <= tables
    downgrade_to_base(settings.database_url)
    tables = set(inspect(create_db_engine(settings.database_url)).get_table_names())
    assert not tables & {"notification_event", "notification_delivery"}
    upgrade_to_head(settings.database_url)


# ---- every sink adapter: conformance -------------------------------------------------------
@dataclass
class Harness:
    port: Any
    received: Callable[[], list[bytes]]
    inject_failure: Callable[[bool], None] | None
    close: Callable[[], None]


def _http(factory: Callable[[str, httpx.Client], Any], decode=None) -> Harness:
    receiver = Receiver(**({"decode": decode} if decode else {})).start()
    client = httpx.Client(timeout=5.0)

    def close() -> None:
        client.close()
        receiver.stop()

    return Harness(factory(receiver.url, client), receiver.payloads, receiver.fail_next, close)


def _log() -> Harness:
    capture = LogCapture()
    logger = logging.getLogger("oran_adapt.notify")
    level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)

    def close() -> None:
        logger.removeHandler(capture)
        logger.setLevel(level)

    return Harness(LogSink(), capture.payloads, None, close)


def _nats() -> Harness:
    server = NatsServer(token="nats-token").start()
    sink = NatsSink(server.url, "oran.adapt.{event_type}", "nats-token", 5.0)
    return Harness(sink, server.payloads, server.fail_next, server.stop)


def _email() -> Harness:
    smtp = SmtpServer()
    sink = EmailSink(host="smtp.test", port=587, starttls=True, username="u", password="p",
                     sender="oran@example.org", recipients=["noc@example.org"], timeout_s=5.0,
                     smtp_factory=smtp)
    return Harness(sink, smtp.payloads, smtp.fail_next, lambda: None)


def _kafka() -> Harness:
    producer = FakeProducer()
    return Harness(KafkaSink(producer, "oran-events", 5.0, (FakeKafkaException,)),
                   producer.payloads, producer.fail_next, lambda: None)


def _aws(make: Callable[[FakeAws], Any]) -> Harness:
    client = FakeAws()
    return Harness(make(client), client.payloads, client.fail_next, lambda: None)


HARNESSES: dict[str, Callable[[], Harness]] = {
    "log": _log,
    "webhook": lambda: _http(lambda url, c: WebhookSink(url, c, 5.0)),
    "slack": lambda: _http(lambda url, c: SlackSink(url, c, 5.0)),
    "pagerduty": lambda: _http(
        lambda url, c: PagerDutySink(url, "rk", "error", "oran-adapt", c, 5.0)),
    "pubsub": lambda: _http(
        lambda url, c: PubSubSink(url.rsplit("/", 1)[0], "proj", "topic", c, lambda: None),
        decode=pubsub_decode),
    "kafka": _kafka,
    "sqs": lambda: _aws(lambda c: SqsSink(c, "https://sqs.local/000/q", (FakeClientError,))),
    "sns": lambda: _aws(lambda c: SnsSink(c, "arn:aws:sns:local:0:t", (FakeClientError,))),
    "nats": _nats,
    "email": _email,
}


@pytest.fixture(params=sorted(HARNESSES))
def harness(request) -> Iterator[tuple[str, Harness]]:
    h = HARNESSES[request.param]()
    try:
        yield request.param, h
    finally:
        h.close()


@pytest.mark.smoke
def test_every_notification_adapter_has_a_harness() -> None:
    assert set(plugins.adapters("notification")) == set(HARNESSES)


def test_conformance(harness) -> None:
    name, h = harness
    ran = run(h.port, Context(received=h.received, inject_failure=h.inject_failure,
                              prefix=name))
    expected = [*CHECKS, *FAILURE_CHECKS] if h.inject_failure else list(CHECKS)
    assert ran == expected


@pytest.mark.smoke
def test_conformance_catches_a_sink_that_swallows_failures() -> None:
    class Lossy:
        def ping(self) -> None:
            return None

        def send(self, message) -> None:
            return None  # reports success for everything, delivers nothing

    with pytest.raises(ConformanceFailure):
        CHECKS["send_delivers"](Lossy(), Context(received=list))
    with pytest.raises(ConformanceFailure):
        FAILURE_CHECKS["temporary_failure_retryable"](
            Lossy(), Context(received=list, inject_failure=lambda retryable: None))


def test_broker_sinks_carry_the_event_id_for_deduplication() -> None:
    ctx = Context(received=list)
    message = ctx.message("dedup")
    fifo = FakeAws()
    SqsSink(fifo, "https://sqs.local/000/q.fifo", (FakeClientError,)).send(message)
    [call] = fifo.calls
    assert call["MessageDeduplicationId"] == message.event_id
    assert call["MessageGroupId"] == message.subject
    assert call["MessageAttributes"][ID_HEADER]["StringValue"] == message.event_id
    server = NatsServer().start()
    try:
        NatsSink(server.url, "oran.{event_type}", None, 5.0).send(message)
        [(subject, headers, body)] = server.messages
        assert subject == "oran.job.completed" and body == message.body
        assert f"Nats-Msg-Id: {message.event_id}".encode() in headers
    finally:
        server.stop()
    mail = EmailSink(host="h", port=25, starttls=False, username=None, password=None,
                     sender="a@example.org", recipients=["b@example.org"],
                     timeout_s=1.0).compose(message)
    assert mail["Message-ID"] == f"<{message.event_id}@example.org>"
    pd = PagerDutySink("http://x", "rk", "error", "src", httpx.Client(), 1.0).payload(message)
    assert pd["dedup_key"] == message.event_id
    assert body_of(message.envelope) == message.body


def test_nats_refuses_a_wrong_token_for_good() -> None:
    server = NatsServer(token="right").start()
    try:
        sink = NatsSink(server.url, "s", "wrong", 5.0)
        with pytest.raises(NotificationDeliveryError) as ei:
            sink.send(Context(received=list).message("auth"))
        assert ei.value.retryable is False
    finally:
        server.stop()


@pytest.mark.smoke
@pytest.mark.parametrize("name,keys", [
    ("log", {}),
    ("webhook", {"notification_webhook_url": "http://127.0.0.1:1/x"}),
    ("slack", {"notification_slack_webhook_url": "http://127.0.0.1:1/x"}),
    ("pagerduty", {"notification_pagerduty_routing_key": "rk"}),
    ("email", {"notification_smtp_host": "smtp.test", "notification_email_from": "a@b.org",
               "notification_email_to": ["c@d.org"]}),
    ("nats", {"notification_nats_url": "nats://127.0.0.1:4222"}),
    ("pubsub", {"notification_pubsub_project": "p", "notification_pubsub_topic": "t",
                "notification_pubsub_credentials": "none"}),
    ("kafka", {"kafka_bootstrap_servers": "127.0.0.1:9092",
               "notification_kafka_topic": "t"}),
    ("sqs", {"notification_sqs_queue_url": "https://sqs.local/0/q",
             "notification_aws_region": "eu-west-1"}),
    ("sns", {"notification_sns_topic_arn": "arn:aws:sns:eu-west-1:0:t",
             "notification_aws_region": "eu-west-1"}),
])
def test_each_factory_builds_its_sink_or_names_the_missing_extra(name, keys, tmp_path) -> None:
    sdk = {"kafka": "confluent_kafka", "sqs": "boto3", "sns": "boto3"}.get(name)
    settings = make_settings(tmp_path, notification_backend=name, **keys)
    if sdk is not None and importlib.util.find_spec(sdk) is None:
        with pytest.raises(ConfigurationError) as ei:
            build_notifiers(settings)
        assert "pip install" in str(ei.value)
        return
    sinks = build_notifiers(settings)
    assert list(sinks) == [name] and hasattr(sinks[name], "send")
