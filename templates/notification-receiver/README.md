# Notification receiver template

A starting point for a service that receives the framework's webhook notifications
(`NOTIFICATION_BACKEND=webhook`). Standard library only.

```sh
RECEIVER_SIGNING_KEYS=whsec_<base64 key> RECEIVER_PORT=8080 python receiver.py
# framework side:
#   NOTIFICATION_BACKEND=webhook
#   NOTIFICATION_WEBHOOK_URL=http://<host>:8080/
#   NOTIFICATION_SIGNING_KEYS=whsec_<the same key>
```

What it does, per `docs/adapters/notification.md`:

* verifies `webhook-signature` (HMAC-SHA256 over `id.timestamp.body`) against every configured
  key and rejects a timestamp outside `RECEIVER_TOLERANCE_S` (default 300): 401 on failure,
  which the framework dead-letters without retrying;
* drops a resend of an event it already handled (the last `RECEIVER_DEDUP_SIZE` ids, default
  10000; delivery is at-least-once);
* answers 204 once `handle()` has run, and 503 if it raised, so the event is sent again.

Replace `handle()` with your code. The dedup memory is per process: behind several replicas,
deduplicate in shared storage on `webhook-id` instead. Put it behind TLS in production.

`scripts/acceptance/phase4.py` checks that `verify()` accepts what the framework signs and
rejects a tampered body.
