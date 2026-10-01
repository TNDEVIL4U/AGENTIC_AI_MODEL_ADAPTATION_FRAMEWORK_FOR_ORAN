"""Notification sink ``email``: one message per event over SMTP submission (STARTTLS by
default). The subject is the one-line summary; the body carries it and the CloudEvents
envelope, and ``Message-ID`` is derived from the event id so a resend after a crash is
recognisably the same message. 4xx SMTP replies and connection failures are retryable, 5xx
replies (a refused sender or recipient) are not."""

from __future__ import annotations

import json
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from oran_adapt.adapters.notify import summary
from oran_adapt.core.errors import ConfigurationError, NotificationDeliveryError
from oran_adapt.ports import AdapterSpec, Capability, OutboundMessage

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def _smtp_error(exc: BaseException, action: str) -> NotificationDeliveryError:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return NotificationDeliveryError(
            f"email {action}: every recipient refused", retryable=all(
                400 <= c < 500 for c in codes), sink="email", status_code=max(codes, default=0),
        )
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return NotificationDeliveryError(
            f"email {action}: SMTP {code}", retryable=code < 500, sink="email", status_code=code,
        )
    return NotificationDeliveryError(
        f"email {action}: {type(exc).__name__}", retryable=True, sink="email"
    )


class EmailSink:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        starttls: bool,
        username: str | None,
        password: str | None,
        sender: str,
        recipients: list[str],
        timeout_s: float,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
    ) -> None:
        self.host = host
        self.port = port
        self.starttls = starttls
        self.username = username
        self._password = password
        self.sender = sender
        self.recipients = recipients
        self.timeout_s = timeout_s
        self._smtp = smtp_factory

    def _session(self) -> Any:
        smtp = self._smtp(self.host, self.port, timeout=self.timeout_s)
        try:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self._password or "")
        except (smtplib.SMTPException, OSError):
            smtp.close()
            raise
        return smtp

    def ping(self) -> None:
        try:
            smtp = self._session()
            try:
                smtp.noop()
            finally:
                smtp.quit()
        except (smtplib.SMTPException, OSError) as exc:
            raise _smtp_error(exc, "ping") from exc

    def compose(self, message: OutboundMessage) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = summary(message)[:200]
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.recipients)
        domain = self.sender.rpartition("@")[2] or "localhost"
        mail["Message-ID"] = f"<{message.event_id}@{domain}>"
        for name, value in message.headers.items():
            mail[f"X-{name}"] = value
        mail.set_content(
            summary(message) + "\n\n" + json.dumps(message.envelope, indent=2, default=str)
        )
        return mail

    def send(self, message: OutboundMessage) -> None:
        mail = self.compose(message)
        try:
            smtp = self._session()
            try:
                smtp.send_message(mail, from_addr=self.sender, to_addrs=self.recipients)
            finally:
                smtp.quit()
        except (smtplib.SMTPException, OSError) as exc:
            raise _smtp_error(exc, "send") from exc


def _email(settings: Settings) -> EmailSink:
    for key in ("notification_smtp_host", "notification_email_from", "notification_email_to"):
        if not getattr(settings, key):
            raise ConfigurationError(
                f"NOTIFICATION_BACKEND=email requires {key.upper()}", key=key.upper()
            )
    password = settings.notification_smtp_password
    return EmailSink(
        host=str(settings.notification_smtp_host),
        port=settings.notification_smtp_port,
        starttls=settings.notification_smtp_starttls,
        username=settings.notification_smtp_username,
        password=password.get_secret_value() if password is not None else None,
        sender=str(settings.notification_email_from),
        recipients=list(settings.notification_email_to),
        timeout_s=settings.notification_timeout_s,
    )


EMAIL = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="email",
        description="one email per event over SMTP submission (STARTTLS by default)",
        features=frozenset({"network", "summary"}),
        config_keys=("notification_smtp_host", "notification_smtp_port",
                     "notification_smtp_starttls", "notification_smtp_username",
                     "notification_smtp_password", "notification_email_from",
                     "notification_email_to", "notification_timeout_s"),
        required_keys=("notification_smtp_host", "notification_email_from",
                       "notification_email_to"),
    ),
    factory=_email,
)
