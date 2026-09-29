"""Outbound notifications: a transactional outbox of job events delivered to the configured
sinks (docs/adapters/notification.md).

- ``events``: writing an event (and one delivery per sink) in the caller's transaction;
- ``signing``: HMAC-SHA256 signatures in the Standard Webhooks format, with key rotation;
- ``dispatcher``: claiming due deliveries, sending, backoff, circuit breaker, dead-letter;
- ``service``: listing deliveries and redriving dead ones.
"""
