"""Notification adapters ``log`` (a structured log line) and ``webhook`` (JSON POST).

Delivery is fire-and-forget: a failed webhook is logged and counted
(``notifications_failed_total``) but never raised into the job that triggered it."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from oran_adapt.core import metrics
from oran_adapt.core.logging import log_event
from oran_adapt.ports import AdapterSpec, Capability, Notification

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger("oran_adapt.notify")


class LogNotifier:
    def notify(self, notification: Notification) -> None:
        log_event(
            logger,
            "notification",
            notification_event=notification.event,
            subject=notification.subject,
            **notification.detail,
        )


class WebhookNotifier:
    def __init__(self, url: str, timeout_s: float) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def notify(self, notification: Notification) -> None:
        body = json.dumps(
            {
                "event": notification.event,
                "subject": notification.subject,
                "detail": notification.detail,
            },
            default=str,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            metrics.NOTIFICATION_FAILURES.labels("webhook").inc()
            log_event(
                logger,
                "webhook notification failed",
                level=logging.WARNING,
                notification_event=notification.event,
                subject=notification.subject,
                cause=str(exc),
            )


def _log(settings: Settings) -> LogNotifier:
    return LogNotifier()


def _webhook(settings: Settings) -> WebhookNotifier:
    if not settings.notification_webhook_url:
        from oran_adapt.core.errors import ConfigurationError

        raise ConfigurationError(
            "NOTIFICATION_BACKEND=webhook requires NOTIFICATION_WEBHOOK_URL",
            key="NOTIFICATION_WEBHOOK_URL",
        )
    return WebhookNotifier(
        settings.notification_webhook_url, settings.notification_webhook_timeout_s
    )


LOG = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="log",
        description="notifications as structured log lines",
        features=frozenset({"offline"}),
    ),
    factory=_log,
)

WEBHOOK = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="webhook",
        description="notifications POSTed as JSON to NOTIFICATION_WEBHOOK_URL",
        features=frozenset({"network"}),
        config_keys=("notification_webhook_url", "notification_webhook_timeout_s"),
        required_keys=("notification_webhook_url",),
    ),
    factory=_webhook,
)
