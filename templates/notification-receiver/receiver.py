"""A receiver for oran-adapt notifications, standard library only.

It verifies each request's Standard Webhooks signature, drops duplicates by event id (delivery
is at-least-once: after a crash the same event is sent again), and answers:

* 2xx - accepted; the dispatcher marks the delivery DELIVERED.
* 401 - bad or stale signature; the dispatcher dead-letters the delivery at once.
* 503 - accepted nothing, try again later (any 5xx, 408, 425 or 429 is retried with backoff).

Run it::

    RECEIVER_SIGNING_KEYS=whsec_... RECEIVER_PORT=8080 python receiver.py

``RECEIVER_SIGNING_KEYS`` takes the same comma-separated value as the framework's
``NOTIFICATION_SIGNING_KEYS``; during a key rotation list both keys on both sides.
Replace ``handle()`` with what your system does with an event.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class InvalidSignature(Exception):
    pass


def parse_keys(spec: str) -> list[bytes]:
    """``whsec_<base64>`` entries are decoded; anything else is used as UTF-8 bytes."""
    keys = []
    for item in (p.strip() for p in spec.split(",")):
        if item:
            keys.append(base64.b64decode(item[6:]) if item.startswith("whsec_")
                        else item.encode("utf-8"))
    return keys


def verify(body: bytes, headers: Mapping[str, str], keys: list[bytes], *,
           tolerance_s: float) -> str:
    """The event id when ``body`` is signed by one of ``keys`` within ``tolerance_s``."""
    lower = {k.lower(): v for k, v in headers.items()}
    msg_id = lower.get("webhook-id")
    timestamp = lower.get("webhook-timestamp")
    signatures = lower.get("webhook-signature")
    if not msg_id or not timestamp or not signatures:
        raise InvalidSignature("missing webhook-id, webhook-timestamp or webhook-signature")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > tolerance_s:
        raise InvalidSignature("timestamp missing or outside the tolerance (replay?)")
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    offered = [s.partition(",")[2] for s in signatures.split() if s.startswith("v1,")]
    for key in keys:
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        if any(hmac.compare_digest(expected, sig) for sig in offered):
            return msg_id
    raise InvalidSignature("no signature matches a known key")


class SeenIds:
    """The last ``size`` event ids, to drop resends."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, event_id: object) -> bool:
        with self._lock:
            return event_id in self._ids

    def add(self, event_id: str) -> None:
        with self._lock:
            self._ids[event_id] = None
            if len(self._ids) > self.size:
                self._ids.popitem(last=False)


def handle(event: dict[str, Any]) -> None:
    """Your code: act on one CloudEvents envelope (``event["type"]``, ``event["data"]``...)."""
    data = event.get("data") or {}
    print(f"{event['type']} {event['subject']}: {data.get('from_status')} -> "
          f"{data.get('to_status')}", flush=True)


def make_handler(keys: list[bytes], tolerance_s: float,
                 seen: SeenIds) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                event_id = verify(body, dict(self.headers.items()), keys,
                                  tolerance_s=tolerance_s)
            except InvalidSignature:
                self._answer(401)
                return
            if event_id not in seen:
                try:
                    handle(json.loads(body))
                except Exception:
                    self._answer(503)  # not handled: ask for a resend
                    return
                seen.add(event_id)  # only once handled, so a failed one is taken again
            self._answer(204)

    return Handler


def main() -> None:
    keys = parse_keys(os.environ["RECEIVER_SIGNING_KEYS"])
    handler = make_handler(keys, float(os.environ.get("RECEIVER_TOLERANCE_S", "300")),
                           SeenIds(int(os.environ.get("RECEIVER_DEDUP_SIZE", "10000"))))
    server = ThreadingHTTPServer((os.environ.get("RECEIVER_HOST", "127.0.0.1"),
                                  int(os.environ.get("RECEIVER_PORT", "8080"))), handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
