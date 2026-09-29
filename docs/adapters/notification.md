# Notification adapters

A notification adapter implements `oran_adapt.ports.NotificationPort` for one sink: somewhere job
state changes are delivered to. `NOTIFICATION_BACKEND` names the sinks (comma-separated, e.g.
`webhook,pagerduty`, or `none`). The core finds adapters through the `oran_adapt.notification`
entry-point group and builds each selected one at the composition root
(`bootstrap.build_notifiers`).

Shipped adapters:

| Adapter | Module | Delivers | Needs | Verified |
|---|---|---|---|---|
| `log` (default) | `adapters/notify.py` | one structured log line per event | - | local |
| `webhook` | `adapters/notify.py` | the signed CloudEvents JSON, POSTed | `NOTIFICATION_WEBHOOK_URL` | local receiver |
| `slack` | `adapters/notify.py` | a one-line summary to an incoming webhook | `NOTIFICATION_SLACK_WEBHOOK_URL` | local receiver, unverified against Slack |
| `pagerduty` | `adapters/notify.py` | an Events API v2 `trigger`, dedup key = event id | `NOTIFICATION_PAGERDUTY_ROUTING_KEY` | local receiver, unverified against PagerDuty |
| `email` | `adapters/notify_email.py` | an email over SMTP, `Message-ID` from the event id | `NOTIFICATION_SMTP_HOST`, `NOTIFICATION_EMAIL_FROM`, `NOTIFICATION_EMAIL_TO` | SMTP double, unverified against a real server |
| `kafka` | `adapters/notify_brokers.py` | one record per event, keyed by job id, headers = signature headers | `KAFKA_BOOTSTRAP_SERVERS`, `NOTIFICATION_KAFKA_TOPIC`; extra `[kafka]` | producer double, unverified against Kafka |
| `sqs` | `adapters/notify_brokers.py` | one message per event; FIFO queues dedupe on the event id | `NOTIFICATION_SQS_QUEUE_URL`, `NOTIFICATION_AWS_REGION`; extra `[aws]` | client double, unverified against AWS |
| `sns` | `adapters/notify_brokers.py` | one publish per event; FIFO topics dedupe on the event id | `NOTIFICATION_SNS_TOPIC_ARN`, `NOTIFICATION_AWS_REGION`; extra `[aws]` | client double, unverified against AWS |
| `pubsub` | `adapters/notify_brokers.py` | a Pub/Sub REST publish, attributes = signature headers | `NOTIFICATION_PUBSUB_PROJECT`, `NOTIFICATION_PUBSUB_TOPIC` | local receiver, unverified against GCP |
| `nats` | `adapters/notify_brokers.py` | a core NATS `HPUB` with `Nats-Msg-Id` = event id | `NOTIFICATION_NATS_URL` | local protocol server, unverified against nats-server |

`GET /api/v1/capabilities` lists every adapter and its keys; `ports.notification.selected` is
the list of configured sinks.

## What gets sent, and when

Every job state transition (`RECEIVED`, `VALIDATING`, ... `COMPLETED`, `FAILED`, `TIMED_OUT`,
`ROLLED_BACK` ...) writes one event of type `job.<status>` (lower case), **in the same database
transaction as the transition itself**. A transition that commits always has its event; a
refused one has none. The event is written to a durable outbox (`notification_event`) together
with one row per receiving sink (`notification_delivery`). `NOTIFICATION_SINK_EVENTS` limits
which types a sink receives; by default `pagerduty` only gets `job.failed`, `job.timed_out` and
`job.rolled_back`, and every other sink gets everything.

The event is a [CloudEvents 1.0](https://cloudevents.io) JSON envelope:

```json
{"specversion": "1.0", "id": "5f0c...", "source": "oran-adapt",
 "type": "oran.adapt.job.failed", "subject": "<job id>", "time": "2026-09-29T12:00:00+00:00",
 "datacontenttype": "application/json",
 "data": {"job_id": "...", "model_id": "...", "from_status": "ADAPTING",
          "to_status": "FAILED", "message": "...", "strategy": "...", "error": {...}}}
```

`NOTIFICATION_SOURCE` and `NOTIFICATION_EVENT_TYPE_PREFIX` set `source` and the `type` prefix.
The webhook body is exactly this JSON (`Content-Type: application/cloudevents+json`); the broker
sinks carry it as the message body. Slack, PagerDuty and email send a summary built from it.

## Delivery guarantees

A dispatcher delivers the outbox: in the API process by default
(`NOTIFICATION_DISPATCH_ENABLED=true`), or as its own process with
`oran-adapt notifications dispatch` (set the flag to false in the API then). Any number of
dispatchers may run against one database.

* **At least once.** A dispatcher claims a delivery with a lease (`NOTIFICATION_LEASE_S`) before
  sending. If it dies mid-send, the lease runs out and another dispatcher sends the event again,
  with the **same event id**. Receivers deduplicate on `webhook-id` (the event id).
* **Retries.** Transport errors, timeouts (`NOTIFICATION_TIMEOUT_S`), 5xx, 408, 425 and 429 are
  retried after `min(NOTIFICATION_BACKOFF_MAX_S, NOTIFICATION_BACKOFF_INITIAL_S * 2^(n-1))`,
  +/- `NOTIFICATION_BACKOFF_JITTER`. Anything else (400, 401, 403, 404 ...) is permanent.
* **Dead letter.** A permanent failure, or `NOTIFICATION_MAX_ATTEMPTS` failed attempts, marks the
  delivery `DEAD` with its last error and status code.
* **Circuit breaker.** `NOTIFICATION_BREAKER_FAILURES` consecutive failures open a sink's
  circuit: its deliveries wait `NOTIFICATION_BREAKER_RESET_S` without spending attempts, then
  one trial delivery decides whether it closes. Other sinks are unaffected.
* **Metrics.** `notification_events_total`, `notification_deliveries_total{sink,outcome}`,
  `notification_delivery_duration_seconds{sink}`, `notification_backlog{status}`,
  `notification_circuit_open{sink}`.

## Inspecting and redriving

```
GET  /api/v1/deliveries?status=DEAD&sink=webhook&event_type=job.failed&subject=<job>&limit=&offset=
GET  /api/v1/deliveries/{id}            # with the event envelope
POST /api/v1/deliveries/{id}/redrive    # admin; 409 unless DEAD
POST /api/v1/deliveries/redrive         # admin; {"sink", "event_type", "subject", "limit"}
```

Redrive moves a `DEAD` delivery back to `PENDING` with a fresh attempt budget, counts it in
`redrive_count` and writes a `NOTIFICATION_REDRIVEN` audit row naming the caller. The CLI has
the same: `oran-adapt notifications list|redrive [--id N] [--sink S] ...`.

## Verifying signatures (receivers)

With `NOTIFICATION_SIGNING_KEYS` set, every message carries
[Standard Webhooks](https://www.standardwebhooks.com) headers (HTTP headers for webhook, record
headers for Kafka, message attributes for SQS/SNS/Pub/Sub, NATS headers):

* `webhook-id`: the event id;
* `webhook-timestamp`: Unix seconds at sending time (fresh on each attempt);
* `webhook-signature`: `v1,<base64 HMAC-SHA256(key, id + "." + timestamp + "." + body)>`, one
  entry per configured key, space-separated.

A receiver recomputes the HMAC with its key over the raw body, compares in constant time,
rejects timestamps outside a tolerance (replay) and answers 401 on a mismatch; the dispatcher
dead-letters a 401 at once. `templates/notification-receiver/receiver.py` does all of this with
the standard library.

Keys are `whsec_<base64>` or raw strings of at least `NOTIFICATION_SIGNING_MIN_KEY_BYTES`
bytes; production refuses to start a `webhook` sink without keys. **Rotation**: set
`NOTIFICATION_SIGNING_KEYS=<old>,<new>` (every message is signed with both), move receivers to
the new key, then drop the old one.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `NOTIFICATION_BACKEND` | `log` | comma-separated sinks, or `none` |
| `NOTIFICATION_SINK_EVENTS` | `{"pagerduty": ["job.failed","job.timed_out","job.rolled_back"]}` | event types per sink (JSON) |
| `NOTIFICATION_SOURCE` | `oran-adapt` | CloudEvents `source` |
| `NOTIFICATION_EVENT_TYPE_PREFIX` | `oran.adapt.` | CloudEvents `type` prefix |
| `NOTIFICATION_DISPATCH_ENABLED` | `true` | run the dispatcher in the API process |
| `NOTIFICATION_DISPATCH_INTERVAL_S` | `1.0` | idle wait between polls |
| `NOTIFICATION_DISPATCH_BATCH` | `50` | deliveries claimed per poll |
| `NOTIFICATION_LEASE_S` | `60` | claim lease; longer than any sink call |
| `NOTIFICATION_MAX_ATTEMPTS` | `8` | attempts before `DEAD` |
| `NOTIFICATION_BACKOFF_INITIAL_S` / `_MAX_S` / `_JITTER` | `2` / `600` / `0.2` | retry backoff |
| `NOTIFICATION_BREAKER_FAILURES` / `_RESET_S` | `5` / `60` | circuit breaker |
| `NOTIFICATION_TIMEOUT_S` | `10` | per sink call |
| `NOTIFICATION_SIGNING_KEYS` | unset | signing keys (secret) |
| `NOTIFICATION_SIGNING_MIN_KEY_BYTES` | `32` | shortest key accepted |
| `NOTIFICATION_WEBHOOK_URL` | unset | webhook target |
| `NOTIFICATION_SLACK_WEBHOOK_URL` | unset | Slack incoming webhook (secret) |
| `NOTIFICATION_PAGERDUTY_ROUTING_KEY` / `_URL` / `_SEVERITY` | unset / Events v2 URL / `error` | PagerDuty |
| `NOTIFICATION_SMTP_HOST` / `_PORT` / `_STARTTLS` / `_USERNAME` / `_PASSWORD` | unset / `587` / `true` / unset / unset | SMTP |
| `NOTIFICATION_EMAIL_FROM` / `NOTIFICATION_EMAIL_TO` | unset / `[]` | email addresses |
| `NOTIFICATION_KAFKA_TOPIC` | unset | Kafka topic (brokers: `KAFKA_BOOTSTRAP_SERVERS`) |
| `NOTIFICATION_SQS_QUEUE_URL` / `NOTIFICATION_SNS_TOPIC_ARN` | unset | AWS targets |
| `NOTIFICATION_AWS_REGION` / `NOTIFICATION_AWS_ENDPOINT_URL` | unset | AWS region / endpoint override |
| `NOTIFICATION_PUBSUB_PROJECT` / `_TOPIC` / `_ENDPOINT` / `_CREDENTIALS` | unset / unset / `https://pubsub.googleapis.com` / `adc` | Pub/Sub |
| `NOTIFICATION_NATS_URL` / `_SUBJECT` / `_TOKEN` | unset / `oran.adapt.{event_type}` / unset | NATS |

A selected sink with a required key unset fails at startup naming the key; so does an unknown
adapter name.

## Writing a new adapter

1. Implement `ping()` and `send(message: OutboundMessage)`. `send` returns only once the sink
   has *accepted* the message. On any failure raise `NotificationDeliveryError(retryable=...)`:
   `True` for anything that may succeed later (unreachable, timeout, throttled, 5xx), `False`
   for what will not (bad credentials, bad request, missing topic). No SDK exception may escape.
   `oran_adapt.adapters.notify` has `post()`, `is_retryable_status()` and `summary()`.
2. Carry `message.event_id` (and `message.headers` where the sink has headers or attributes) so
   receivers can deduplicate and verify; accept the same message twice (at-least-once).
3. Import the vendor SDK inside the factory only, under `oran_adapt/adapters/`, and add it to
   `SDK_HOMES` in `tests/unit/test_import_boundary.py`. A missing SDK is a `ConfigurationError`
   naming the `pip install` extra.
4. Declare an `AdapterSpec` with its `config_keys` and `required_keys`; add Settings fields for
   new keys; register it under `[project.entry-points."oran_adapt.notification"]`.
5. Run the conformance suite against a local receiving end (see below) and list the adapter in
   the table above.

## Conformance suite

`oran_adapt.conformance.notification` checks an adapter against a `Context` whose `received()`
returns what the receiving end got:

* `protocol`, `ping` (delivers nothing), `send_delivers` (exactly one payload naming the job),
  `resend_accepted` (a resend is delivered again), `event_types`;
* with `Context.inject_failure(retryable)`: `temporary_failure_retryable`,
  `permanent_failure_not_retryable`, `recovers_after_failure`.

```python
from oran_adapt.conformance.notification import Context, run
run(MySink(...), Context(received=my_receiver.payloads, inject_failure=my_receiver.fail_next))
```

`tests/unit/test_phase4_notifications.py` runs it for every shipped adapter with the doubles in
`tests/unit/notification_doubles.py`.
