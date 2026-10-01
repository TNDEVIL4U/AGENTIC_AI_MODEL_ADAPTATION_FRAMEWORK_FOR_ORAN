# Hardening Phase 4 report: outbound notifications

Branch `phase14-production-hardening`. Everything below marked "passed" was run locally on
Windows 11, Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. No real sink was used:
`webhook`, `slack`, `pagerduty` and `pubsub` were tested against a local HTTP receiver, `nats`
against a local server that speaks the NATS protocol, and `email`, `kafka`, `sqs` and `sns`
against client doubles (`tests/unit/notification_doubles.py`). **All nine non-default adapters
are unverified against the real services.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 3: no outbound notifications. Callers had to poll `GET /jobs/{id}` or block on the POST | every job state transition writes a `job.<status>` event (CloudEvents 1.0) **in the transition's own transaction** (`notifications/events.record_job_transition`, called from each `AdaptationEvent` writer in `orchestrator/jobs.py`). A committed transition always has its event, and a refused one has none |
| Events must survive a crash | durable outbox: `notification_event` plus one `notification_delivery` row per receiving sink (migration `0007_notification_outbox`). `notifications/dispatcher.py` claims due rows with a lease and counts the attempt at claim time. It records an outcome only while it still holds the lease, so a stale worker cannot overwrite a newer result. A delivery whose worker died is re-claimed when its lease expires. Delivery is at least once |
| Receivers cannot tell a real event from a forged one | Standard Webhooks HMAC-SHA256 signing (`notifications/signing.py`): `webhook-id`, `webhook-timestamp` and `webhook-signature` headers, with one `v1,` signature per key in `NOTIFICATION_SIGNING_KEYS`, so keys can be rotated without downtime. Keys shorter than `NOTIFICATION_SIGNING_MIN_KEY_BYTES` are refused. Production refuses a `webhook` sink without keys |
| A failing sink must not lose events or stall the others | exponential backoff with jitter; retries only 5xx, 408, 425, 429, transport errors and timeouts; dead-letter after `NOTIFICATION_MAX_ATTEMPTS`. A per-sink circuit breaker holds a sink's deliveries while its circuit is open, without using up their attempts |
| Dead letters need an operator path | `GET /api/v1/deliveries` (filters: status, sink, event type, subject; pages), `GET /api/v1/deliveries/{id}`, `POST /api/v1/deliveries/{id}/redrive`, `POST /api/v1/deliveries/redrive` (bulk). Redrive needs ADMIN, answers 409 unless the delivery is DEAD, and writes an audit row. CLI: `oran-adapt notifications dispatch | list | redrive` |
| One sink, hardwired | ten adapters behind `oran_adapt.notification` entry points. `NOTIFICATION_BACKEND` takes a comma-separated list, and `NOTIFICATION_SINK_EVENTS` routes event types per sink |
| No definition of "a correct notification adapter" | conformance suite `oran_adapt.conformance.notification` |
| Extension path | authoring guide `docs/adapters/notification.md` (envelope, guarantees, signature verification, redrive, keys); receiver template `templates/notification-receiver/`; defaults in `docs/OPEN-QUESTIONS.md` ("Notification adapters") |

## 2. Ports and adapters

`NotificationPort` (`ports/runtime.py`): `send(event, headers)` raises
`NotificationDeliveryError(retryable=...)` on failure.

| Adapter | Delivers | Required keys | Test double |
|---|---|---|---|
| `log` (default) | one structured log line per event | none | captured log |
| `webhook` | signed CloudEvents JSON, POSTed as `application/cloudevents+json` | `NOTIFICATION_WEBHOOK_URL` (+ `NOTIFICATION_SIGNING_KEYS` in production) | local `Receiver` that verifies signatures |
| `slack` | a one-line summary to an incoming webhook | `NOTIFICATION_SLACK_WEBHOOK_URL` | local receiver |
| `pagerduty` | Events API v2 `trigger`; `dedup_key` = event id | `NOTIFICATION_PAGERDUTY_ROUTING_KEY` | local receiver |
| `email` | SMTP (STARTTLS by default); `Message-ID` from the event id | `NOTIFICATION_SMTP_HOST`, `NOTIFICATION_EMAIL_FROM`, `NOTIFICATION_EMAIL_TO` | SMTP client double |
| `kafka` | one record per event, keyed by job id; signature headers as record headers (needs `confluent-kafka`) | `KAFKA_BOOTSTRAP_SERVERS`, `NOTIFICATION_KAFKA_TOPIC` | producer double |
| `sqs` | one message; `MessageAttributes` = signature headers; FIFO queues deduplicate on the event id (needs `boto3`) | `NOTIFICATION_SQS_QUEUE_URL`, `NOTIFICATION_AWS_REGION` | client double |
| `sns` | one publish, with the same attributes and FIFO behaviour as `sqs` | `NOTIFICATION_SNS_TOPIC_ARN`, `NOTIFICATION_AWS_REGION` | client double |
| `pubsub` | Pub/Sub REST publish; attributes = signature headers | `NOTIFICATION_PUBSUB_PROJECT`, `NOTIFICATION_PUBSUB_TOPIC` | local receiver |
| `nats` | core NATS `HPUB` with `Nats-Msg-Id` = event id (stdlib socket client) | `NOTIFICATION_NATS_URL` | local NATS-protocol server |

`boto3`/`botocore` are allowed in `adapters/notify_brokers.py`, and `confluent_kafka` there
as well as in `adapters/kafka_cdc.py` (import-boundary test). An adapter whose SDK is missing
fails at start-up with a `ConfigurationError` that names the `pip install` extra.

New metrics: `notification_events_total{event_type}`, `notification_deliveries_total`,
`notification_delivery_duration_seconds{sink}`, `notification_backlog{status}`,
`notification_circuit_open{sink}`.

## 3. Configuration keys added in Phase 4

Common: `NOTIFICATION_BACKEND` (`log`), `NOTIFICATION_SINK_EVENTS`, `NOTIFICATION_SOURCE`
(`oran-adapt`), `NOTIFICATION_EVENT_TYPE_PREFIX` (`oran.adapt.`),
`NOTIFICATION_DISPATCH_ENABLED` (true), `NOTIFICATION_DISPATCH_INTERVAL_S` (1),
`NOTIFICATION_DISPATCH_BATCH` (50), `NOTIFICATION_LEASE_S` (60),
`NOTIFICATION_MAX_ATTEMPTS` (8), `NOTIFICATION_BACKOFF_INITIAL_S` (2),
`NOTIFICATION_BACKOFF_MAX_S` (600), `NOTIFICATION_BACKOFF_JITTER` (0.2),
`NOTIFICATION_BREAKER_FAILURES` (5), `NOTIFICATION_BREAKER_RESET_S` (60),
`NOTIFICATION_TIMEOUT_S` (10), `NOTIFICATION_SIGNING_KEYS`,
`NOTIFICATION_SIGNING_MIN_KEY_BYTES` (32).

Per adapter: `NOTIFICATION_WEBHOOK_URL`; `NOTIFICATION_SLACK_WEBHOOK_URL`;
`NOTIFICATION_PAGERDUTY_ROUTING_KEY`, `NOTIFICATION_PAGERDUTY_URL`,
`NOTIFICATION_PAGERDUTY_SEVERITY`; `NOTIFICATION_SMTP_HOST`, `NOTIFICATION_SMTP_PORT`,
`NOTIFICATION_SMTP_STARTTLS`, `NOTIFICATION_SMTP_USERNAME`, `NOTIFICATION_SMTP_PASSWORD`,
`NOTIFICATION_EMAIL_FROM`, `NOTIFICATION_EMAIL_TO`; `NOTIFICATION_KAFKA_TOPIC`;
`NOTIFICATION_SQS_QUEUE_URL`, `NOTIFICATION_SNS_TOPIC_ARN`, `NOTIFICATION_AWS_REGION`,
`NOTIFICATION_AWS_ENDPOINT_URL`; `NOTIFICATION_PUBSUB_PROJECT`, `NOTIFICATION_PUBSUB_TOPIC`,
`NOTIFICATION_PUBSUB_ENDPOINT`, `NOTIFICATION_PUBSUB_CREDENTIALS`; `NOTIFICATION_NATS_URL`,
`NOTIFICATION_NATS_SUBJECT`, `NOTIFICATION_NATS_TOKEN`.

The service refuses to start on any of these: a missing required key; an unknown sink name in
`NOTIFICATION_BACKEND` or `NOTIFICATION_SINK_EVENTS`; a signing key that is malformed or too
short; `ENVIRONMENT=production` with a `webhook` sink and no signing keys. The config-file lint
counts secret-typed keys (signing keys among them) as supplied from outside the file, so
`config/examples/production.toml` passes it. `NOTIFICATION_BACKEND=none` turns the port off.
With the defaults, events are written and logged, and nothing leaves the host.

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Fake-receiver suite: normal delivery | PASS | `SCENARIOS["normal_delivery"]` in `test_phase4_notifications.py`: delivered once, signature verified by the receiver |
| 2 | 500s: retried with backoff, then delivered; dead-lettered when they persist | PASS | `500_then_success`, `500s_dead_letter` |
| 3 | Timeout counts as a retryable failure | PASS | `timeout` (receiver delay 2 s, sink timeout 0.3 s) |
| 4 | Down-then-up: dead-lettered while the receiver is down, delivered after redrive through the API | PASS | `down_then_up_redrive`: 3 attempts, DEAD, listed by `GET /deliveries?status=DEAD`, redrive 200, a second redrive 409, then DELIVERED, plus an audit row |
| 5 | Bad signature is rejected, not retried | PASS | `bad_signature`: the receiver holds a different key, answers 401, and the delivery is DEAD after 1 attempt |
| 6 | API killed mid-delivery: no event lost | PASS | `api_killed_mid_delivery` (`@pytest.mark.heavy`, run by acceptance check 1): a uvicorn subprocess is killed while the receiver holds its POST; a new dispatcher cannot claim it until the lease expires, then delivers the same event id on attempt 2 |
| 7 | An event for every state transition, in the same transaction | PASS | `assert_every_transition_notified` (smoke tier); acceptance check 2 |
| 8 | Every notification adapter passes the conformance suite | PASS (receiver/doubles) | `test_conformance[<adapter>]` for all 10; `test_every_notification_adapter_has_a_harness`; acceptance check 3 |
| 9 | Signing rotation; circuit breaker holds deliveries without spending attempts; stale workers cannot overwrite outcomes | PASS | the signing tests; `test_open_circuit_holds_deliveries_without_spending_attempts`; the stale-worker `_finish` test |
| 10 | Vendor SDKs only inside adapters; every adapter documented; the receiver template verifies real signatures | PASS | `test_import_boundary.py`; acceptance check 4 (loads `templates/notification-receiver/receiver.py` and checks `verify()` against `sign()` output and a tampered body) |
| 11 | Migrations have a tested rollback | PASS | migration 0007 upgrade and downgrade test in `test_phase4_notifications.py` |

## 5. Hardcoding

**Removed:** nothing was open in this area. There were no notifications before this phase.
Every address, credential, timeout, retry, backoff, breaker, lease, batch and routing value
in the new code is a `NOTIFICATION_*` key.

**Kept on purpose** (see `docs/hardcoding-inventory.md`, "Hardening Phase 4 status"):
- protocol facts: the NATS default port 4222, the PagerDuty 1024-character summary limit, and the HTTP statuses that mean "try again later";
- storage bounds: `leased_by` at 100 characters, `last_error` at 2000, the email subject at 200, and at most 100 delivery ids per redrive audit row.

**Remaining:**
- Promotions and manual rollbacks made outside a job (`POST /models/{id}/rollback`) do not emit
  events yet (recorded in `docs/OPEN-QUESTIONS.md`).
- Inventory burn-down: C open 8 → 8 (no C item was in this area).

## 6. Assumptions and defaults

Recorded in `docs/OPEN-QUESTIONS.md` ("Notification adapters"):
- The default sink is `log`, so nothing leaves the host.
- The dispatcher runs inside the API; a separate `notifications dispatch` process is also supported.
- Delivery is at least once, and receivers deduplicate on `webhook-id`.
- Retries: 8 attempts, backoff from 2 s doubling up to 10 min, with ±20% jitter.
- A sink's circuit opens after 5 consecutive failures and stays open for 60 s.
- The lease is 60 s, longer than the 10 s send timeout.
- PagerDuty receives only failures, timeouts and rollbacks.
- Only the webhook sink must be signed in production. The others rely on their own authentication.

## 7. Unverified locally

- **Every non-default adapter against its real service.** Kafka, AWS SQS/SNS, Google Pub/Sub,
  nats-server, an SMTP server, Slack and PagerDuty were not available. The receiver, the NATS
  protocol server and the client doubles implement the APIs as the adapters use them. They are
  not recordings of the real services. `boto3` and `confluent-kafka` are not installed. Their
  adapters were tested with injected clients, and the missing-SDK error path was tested.
- `templates/notification-receiver/receiver.py` has not been run as a deployed service. Only its
  `verify()` is tested against the framework's signatures.
- Several API replicas each running a dispatcher against PostgreSQL. The claim protocol was
  exercised on SQLite, with two dispatchers in one process and one killed API process.
- STARTTLS and SMTP authentication against a real server. The adapter speaks SMTP submission
  (STARTTLS) only. Implicit TLS on port 465 is not supported.

## 8. Gate

`scripts/verify.sh 4`: **PASS in 247 s** (ruff, mypy 0 errors, import boundary, no-gaps lint,
scoped tests and the smoke tier with 2 workers, acceptance 4/4), run locally on 2026-09-29.
