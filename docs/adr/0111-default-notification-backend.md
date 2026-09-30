# ADR-0111: Default NOTIFICATION_BACKEND is `log`

- **Status:** Accepted
- **Selector:** `NOTIFICATION_BACKEND`
- **Default:** `log`
- **Alternatives shipped:** `email`, `kafka`, `nats`, `pagerduty`, `pubsub`, `slack`, `sns`, `sqs`, `webhook`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#notification-adapters)

## Context

Job and rollout events should reach the operations team and other systems. Which channel is watched is unknown.

## Decision

`log`: events are written to the durable outbox and logged; nothing leaves the host. Nine sinks ship (webhook, Slack, PagerDuty, email, Kafka, SQS, SNS, Pub/Sub, NATS), several at once if comma-separated.

## Consequences

No outbound traffic by default; delivery is at least once with retries and a dead-letter state once a sink is chosen.

## Revisit when

The operations team names its channel.
