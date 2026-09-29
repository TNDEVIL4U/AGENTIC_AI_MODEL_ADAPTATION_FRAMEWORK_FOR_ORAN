"""HMAC-SHA256 message signatures in the Standard Webhooks format (standardwebhooks.com).

Three headers go with every signed message:

- ``webhook-id``: the event id, the same on every retry (receivers deduplicate on it);
- ``webhook-timestamp``: Unix seconds when this attempt was signed;
- ``webhook-signature``: ``v1,<base64 HMAC>`` for each signing key, space-separated.

The HMAC is over ``{webhook-id}.{webhook-timestamp}.{body}``. Every configured key signs, so a
key is rotated without a gap: add the new key (both sign), move receivers to it, drop the old.
A receiver accepts a message when any signature matches any key it holds and the timestamp is
within its tolerance (replay protection). Standard library only, so receivers can copy
``verify`` as it is (templates/notification-receiver).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping

from oran_adapt.core.errors import ConfigurationError, SignatureVerificationError

ID_HEADER = "webhook-id"
TIMESTAMP_HEADER = "webhook-timestamp"
SIGNATURE_HEADER = "webhook-signature"
KEY_PREFIX = "whsec_"
VERSION = "v1"


def parse_keys(spec: str, min_bytes: int) -> list[bytes]:
    """Signing keys from their comma-separated configuration form. ``whsec_<base64>`` is
    decoded; anything else is taken as the key's UTF-8 bytes. A key shorter than
    ``min_bytes`` is refused (a short HMAC key is guessable)."""
    keys: list[bytes] = []
    for position, item in enumerate(p.strip() for p in spec.split(",")):
        if not item:
            continue
        if item.startswith(KEY_PREFIX):
            try:
                key = base64.b64decode(item[len(KEY_PREFIX):], validate=True)
            except (binascii.Error, ValueError):
                raise ConfigurationError(
                    f"NOTIFICATION_SIGNING_KEYS entry {position + 1} is not valid base64 after "
                    f"{KEY_PREFIX}",
                    key="NOTIFICATION_SIGNING_KEYS",
                ) from None
        else:
            key = item.encode("utf-8")
        if len(key) < min_bytes:
            raise ConfigurationError(
                f"NOTIFICATION_SIGNING_KEYS entry {position + 1} is {len(key)} bytes; at least "
                f"{min_bytes} (NOTIFICATION_SIGNING_MIN_KEY_BYTES) are required",
                key="NOTIFICATION_SIGNING_KEYS",
            )
        keys.append(key)
    return keys


def _mac(key: bytes, msg_id: str, timestamp: str, body: bytes) -> str:
    signed = msg_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def sign(msg_id: str, body: bytes, keys: list[bytes], timestamp: int | None = None
         ) -> dict[str, str]:
    """The three signature headers for ``body`` (no ``webhook-signature`` without keys)."""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    headers = {ID_HEADER: msg_id, TIMESTAMP_HEADER: ts}
    if keys:
        headers[SIGNATURE_HEADER] = " ".join(
            f"{VERSION},{_mac(key, msg_id, ts, body)}" for key in keys
        )
    return headers


def verify(body: bytes, headers: Mapping[str, str], keys: list[bytes], *,
           tolerance_s: float, now: float | None = None) -> str:
    """The message id when ``body`` carries a valid signature by one of ``keys`` and a
    timestamp within ``tolerance_s`` of ``now``; SignatureVerificationError otherwise.
    Header names are matched case-insensitively."""
    lower = {k.lower(): v for k, v in headers.items()}
    msg_id = lower.get(ID_HEADER)
    timestamp = lower.get(TIMESTAMP_HEADER)
    signatures = lower.get(SIGNATURE_HEADER)
    if not msg_id or not timestamp or not signatures:
        raise SignatureVerificationError(
            "missing signature headers",
            required=[ID_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER],
        )
    try:
        sent = int(timestamp)
    except ValueError:
        raise SignatureVerificationError("webhook-timestamp is not an integer") from None
    current = time.time() if now is None else now
    if abs(current - sent) > tolerance_s:
        raise SignatureVerificationError(
            "webhook-timestamp is outside the tolerance", tolerance_s=tolerance_s
        )
    offered = [
        sig for version, _, sig in (s.partition(",") for s in signatures.split())
        if version == VERSION
    ]
    for key in keys:
        expected = _mac(key, msg_id, timestamp, body)
        if any(hmac.compare_digest(expected, sig) for sig in offered):
            return msg_id
    raise SignatureVerificationError("no signature matches a known key")
