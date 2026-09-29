"""Notification sinks over HTTP and the log: ``log``, ``webhook`` (signed CloudEvents POST),
``slack`` (incoming webhook) and ``pagerduty`` (Events API v2).

Every failure leaves as NotificationDeliveryError: transport errors, timeouts, 5xx, 408, 425
and 429 are retryable; any other refusal (bad signature, bad request, auth) is not. The
dispatcher (oran_adapt.notifications.dispatcher) decides what happens next. URLs are never put
into error text (a Slack URL is its own credential).
"""

from __future__ import annotations

import json
import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from oran_adapt.core.errors import ConfigurationError, NotificationDeliveryError
from oran_adapt.core.logging import log_event
from oran_adapt.ports import AdapterSpec, Capability, OutboundMessage

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger("oran_adapt.notify")

CLOUDEVENTS_JSON = "application/cloudevents+json"
_RETRYABLE_STATUS = frozenset({408, 425, 429})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_retryable_status(status: int) -> bool:
    return status >= 500 or status in _RETRYABLE_STATUS


def post(client: httpx.Client, url: str, *, sink: str, content: bytes,
         headers: dict[str, str]) -> httpx.Response:
    """POST and map every failure to NotificationDeliveryError."""
    try:
        response = client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as exc:
        raise NotificationDeliveryError(
            f"{sink} timed out", retryable=True, sink=sink, reason="timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(
            f"{sink} unreachable: {type(exc).__name__}", retryable=True, sink=sink,
            reason="transport",
        ) from exc
    if response.status_code >= 300:
        raise NotificationDeliveryError(
            f"{sink} answered HTTP {response.status_code}",
            retryable=is_retryable_status(response.status_code),
            sink=sink,
            status_code=response.status_code,
        )
    return response


def tcp_ping(url: str, timeout_s: float, *, sink: str) -> None:
    """Reachability without sending a message: a TCP connection to the URL's host and port."""
    parts = urlsplit(url)
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    if not parts.hostname or port is None:
        raise NotificationDeliveryError(f"{sink} URL has no host or port", retryable=False,
                                        sink=sink)
    try:
        socket.create_connection((parts.hostname, port), timeout=timeout_s).close()
    except OSError as exc:
        raise NotificationDeliveryError(
            f"{sink} unreachable: {type(exc).__name__}", retryable=True, sink=sink
        ) from exc


def summary(message: OutboundMessage) -> str:
    """One human line for chat and paging sinks."""
    data = message.envelope.get("data") or {}
    text = data.get("message") or ""
    error = (data.get("error") or {}).get("code")
    model = data.get("model_id")
    parts = [f"[{message.event_type}] job {message.subject}"]
    if model:
        parts.append(f"model {model}")
    if error:
        parts.append(f"error {error}")
    line = ", ".join(parts)
    return f"{line}: {text}" if text else line


class LogSink:
    """Each event as one structured log line: always accepted."""

    def ping(self) -> None:
        log_event(logger, "notification log sink ready", logging.DEBUG, sink="log")

    def send(self, message: OutboundMessage) -> None:
        log_event(
            logger, summary(message), event_id=message.event_id,
            event_type=message.event_type, sink="log",
        )


class WebhookSink:
    """The CloudEvents envelope POSTed as ``application/cloudevents+json``, with the signature
    headers (oran_adapt.notifications.signing)."""

    def __init__(self, url: str, client: httpx.Client, timeout_s: float) -> None:
        self.url = url
        self.client = client
        self.timeout_s = timeout_s

    def ping(self) -> None:
        tcp_ping(self.url, self.timeout_s, sink="webhook")

    def send(self, message: OutboundMessage) -> None:
        post(self.client, self.url, sink="webhook", content=message.body,
             headers={"Content-Type": CLOUDEVENTS_JSON, **message.headers})


class SlackSink:
    """A one-line summary posted to a Slack incoming webhook."""

    def __init__(self, url: str, client: httpx.Client, timeout_s: float) -> None:
        self._url = url
        self.client = client
        self.timeout_s = timeout_s

    def ping(self) -> None:
        tcp_ping(self._url, self.timeout_s, sink="slack")

    def send(self, message: OutboundMessage) -> None:
        body = json.dumps({"text": summary(message)}).encode("utf-8")
        post(self.client, self._url, sink="slack", content=body,
             headers={"Content-Type": "application/json"})


class PagerDutySink:
    """A PagerDuty Events API v2 ``trigger``. The dedup key is the event id, so a resend after
    a crash does not open a second incident."""

    def __init__(self, url: str, routing_key: str, severity: str, source: str,
                 client: httpx.Client, timeout_s: float) -> None:
        self.url = url
        self._routing_key = routing_key
        self.severity = severity
        self.source = source
        self.client = client
        self.timeout_s = timeout_s

    def ping(self) -> None:
        tcp_ping(self.url, self.timeout_s, sink="pagerduty")

    def payload(self, message: OutboundMessage) -> dict[str, Any]:
        return {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "dedup_key": message.event_id,
            "payload": {
                "summary": summary(message)[:1024],
                "source": self.source,
                "severity": self.severity,
                "timestamp": message.envelope.get("time"),
                "component": message.subject,
                "class": message.event_type,
                "custom_details": message.envelope.get("data"),
            },
        }

    def send(self, message: OutboundMessage) -> None:
        post(self.client, self.url, sink="pagerduty",
             content=json.dumps(self.payload(message), default=str).encode("utf-8"),
             headers={"Content-Type": "application/json"})


def http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(timeout=settings.notification_timeout_s, follow_redirects=False)


def _required(settings: Settings, key: str, adapter: str) -> Any:
    value = getattr(settings, key)
    if value is None or value == "" or value == []:
        raise ConfigurationError(
            f"NOTIFICATION_BACKEND={adapter} requires {key.upper()}", key=key.upper()
        )
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _log(settings: Settings) -> LogSink:
    return LogSink()


def _webhook(settings: Settings) -> WebhookSink:
    return WebhookSink(_required(settings, "notification_webhook_url", "webhook"),
                       http_client(settings), settings.notification_timeout_s)


def _slack(settings: Settings) -> SlackSink:
    return SlackSink(_required(settings, "notification_slack_webhook_url", "slack"),
                     http_client(settings), settings.notification_timeout_s)


def _pagerduty(settings: Settings) -> PagerDutySink:
    return PagerDutySink(
        settings.notification_pagerduty_url,
        _required(settings, "notification_pagerduty_routing_key", "pagerduty"),
        settings.notification_pagerduty_severity,
        settings.notification_source,
        http_client(settings),
        settings.notification_timeout_s,
    )


LOG = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="log",
        description="each event as a structured log line",
        features=frozenset({"offline"}),
    ),
    factory=_log,
)

WEBHOOK = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="webhook",
        description="signed CloudEvents JSON POSTed to NOTIFICATION_WEBHOOK_URL",
        features=frozenset({"network", "signed", "envelope"}),
        config_keys=("notification_webhook_url", "notification_timeout_s",
                     "notification_signing_keys"),
        required_keys=("notification_webhook_url",),
        production_keys=("notification_signing_keys",),
    ),
    factory=_webhook,
)

SLACK = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="slack",
        description="a one-line summary per event to a Slack incoming webhook",
        features=frozenset({"network", "summary"}),
        config_keys=("notification_slack_webhook_url", "notification_timeout_s"),
        required_keys=("notification_slack_webhook_url",),
    ),
    factory=_slack,
)

PAGERDUTY = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="pagerduty",
        description="a PagerDuty Events API v2 trigger per event (dedup key: the event id)",
        features=frozenset({"network", "summary", "paging"}),
        config_keys=("notification_pagerduty_routing_key", "notification_pagerduty_url",
                     "notification_pagerduty_severity", "notification_timeout_s"),
        required_keys=("notification_pagerduty_routing_key",),
    ),
    factory=_pagerduty,
)
