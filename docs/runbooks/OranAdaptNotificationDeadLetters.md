# OranAdaptNotificationDeadLetters

**Fires when** dead-lettered notification deliveries (`notification_backlog{status="DEAD"}`)
have been present for 15 minutes.

**Impact.** Subscribers of the affected sinks missed events (job transitions, rollouts). The
framework itself is unaffected.

**Check.**
1. `notification_deliveries_total` by `sink` and `outcome`, and `notification_circuit_open` by
   `sink`: which sink fails.
2. `GET /api/v1/deliveries?status=DEAD`: each delivery's last error.
3. The sink's endpoint and credentials (webhook URL, broker, SMTP server).

**Fix.** Restore the sink, then redrive the dead letters: `POST /api/v1/deliveries/redrive`
(all of them) or `POST /api/v1/deliveries/{delivery_id}/redrive` (one).
