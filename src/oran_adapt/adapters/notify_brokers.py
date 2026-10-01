"""Notification sinks on message brokers: ``kafka``, ``sqs``, ``sns``, ``pubsub`` and ``nats``.

Each carries the CloudEvents envelope as the message body and the signature headers
(``webhook-id``, ``webhook-timestamp``, ``webhook-signature``) as message headers or
attributes, so a consumer verifies it exactly as a webhook receiver does and deduplicates on
``webhook-id``. The vendor clients are injected (tests use fakes and a local NATS stand-in);
the factories import the optional SDKs lazily (confluent-kafka, boto3) and name the extra to
install when one is missing. Pub/Sub (REST) and NATS (its text protocol over TCP) need no SDK.
"""

from __future__ import annotations

import base64
import json
import socket
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from oran_adapt.adapters.notify import http_client, is_retryable_status, post
from oran_adapt.core.errors import ConfigurationError, NotificationDeliveryError
from oran_adapt.ports import AdapterSpec, Capability, OutboundMessage

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def _missing_sdk(adapter: str, module: str, extra: str) -> ConfigurationError:
    return ConfigurationError(
        f"NOTIFICATION_BACKEND={adapter} needs the {module} package "
        f"(pip install 'oran-adapt[{extra}]')",
        key="NOTIFICATION_BACKEND",
    )


def _required(value: Any, key: str, adapter: str) -> Any:
    if value is None or value == "":
        raise ConfigurationError(
            f"NOTIFICATION_BACKEND={adapter} requires {key.upper()}", key=key.upper()
        )
    return value


# ---- Kafka ------------------------------------------------------------------------------
class KafkaSink:
    """One record per event on NOTIFICATION_KAFKA_TOPIC, keyed by the job id (so a job's
    events stay in order within a partition). ``producer`` is anything with
    confluent_kafka.Producer's produce/flush/list_topics; ``errors`` are the client's
    exception types."""

    def __init__(self, producer: Any, topic: str, timeout_s: float,
                 errors: tuple[type[BaseException], ...]) -> None:
        self.producer = producer
        self.topic = topic
        self.timeout_s = timeout_s
        self.errors = errors
        self._produce_errors: tuple[type[BaseException], ...] = (*errors, BufferError)

    def ping(self) -> None:
        try:
            self.producer.list_topics(self.topic, timeout=self.timeout_s)
        except self.errors as exc:
            raise NotificationDeliveryError(
                f"kafka unreachable: {exc}", retryable=True, sink="kafka"
            ) from exc

    def send(self, message: OutboundMessage) -> None:
        outcome: dict[str, Any] = {}

        def _delivered(err: Any, _msg: Any) -> None:
            outcome["error"] = err
            outcome["done"] = True

        try:
            self.producer.produce(
                self.topic,
                key=message.subject.encode("utf-8"),
                value=message.body,
                headers=[(k, v.encode("utf-8")) for k, v in message.headers.items()],
                on_delivery=_delivered,
            )
            remaining = self.producer.flush(self.timeout_s)
        except self._produce_errors as exc:
            raise NotificationDeliveryError(
                f"kafka produce failed: {exc}", retryable=True, sink="kafka"
            ) from exc
        if remaining or not outcome.get("done"):
            raise NotificationDeliveryError(
                "kafka did not acknowledge the record in time", retryable=True, sink="kafka",
                reason="timeout",
            )
        err = outcome.get("error")
        if err is not None:
            retriable = getattr(err, "retriable", None)
            raise NotificationDeliveryError(
                f"kafka refused the record: {err}",
                retryable=bool(retriable()) if callable(retriable) else True,
                sink="kafka",
            )


def _kafka(settings: Settings) -> KafkaSink:
    try:
        from confluent_kafka import KafkaException, Producer
    except ImportError:
        raise _missing_sdk("kafka", "confluent-kafka", "kafka") from None
    servers = _required(settings.kafka_bootstrap_servers, "kafka_bootstrap_servers", "kafka")
    topic = _required(settings.notification_kafka_topic, "notification_kafka_topic", "kafka")
    producer = Producer({
        "bootstrap.servers": servers,
        "enable.idempotence": True,
        "acks": "all",
        "message.timeout.ms": int(settings.notification_timeout_s * 1000),
    })
    return KafkaSink(producer, topic, settings.notification_timeout_s, (KafkaException,))


# ---- AWS SQS / SNS ------------------------------------------------------------------------
def aws_error(exc: BaseException, sink: str, action: str) -> NotificationDeliveryError:
    """A botocore error as a NotificationDeliveryError: throttling and 5xx are retryable,
    other client errors (access denied, no such queue) are not; transport errors are."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = response.get("Error", {}).get("Code", "")
        throttled = "Throttl" in code or code in {"RequestLimitExceeded", "ServiceUnavailable"}
        retryable = throttled or not isinstance(status, int) or status >= 500
        return NotificationDeliveryError(
            f"{sink} {action} failed: {code or type(exc).__name__}", retryable=retryable,
            sink=sink, status_code=status, aws_code=code,
        )
    return NotificationDeliveryError(
        f"{sink} {action} failed: {type(exc).__name__}", retryable=True, sink=sink
    )


def _aws_attributes(message: OutboundMessage) -> dict[str, dict[str, str]]:
    attrs = {k: v for k, v in message.headers.items()}
    attrs["event-type"] = message.event_type
    return {k: {"DataType": "String", "StringValue": v} for k, v in attrs.items()}


class SqsSink:
    """``SendMessage`` to NOTIFICATION_SQS_QUEUE_URL; a FIFO queue (``.fifo``) groups by job id
    and deduplicates on the event id."""

    def __init__(self, client: Any, queue_url: str,
                 errors: tuple[type[BaseException], ...]) -> None:
        self.client = client
        self.queue_url = queue_url
        self.errors = errors

    def ping(self) -> None:
        try:
            self.client.get_queue_attributes(QueueUrl=self.queue_url,
                                             AttributeNames=["QueueArn"])
        except self.errors as exc:
            raise aws_error(exc, "sqs", "GetQueueAttributes") from exc

    def send(self, message: OutboundMessage) -> None:
        kwargs: dict[str, Any] = {
            "QueueUrl": self.queue_url,
            "MessageBody": message.body.decode("utf-8"),
            "MessageAttributes": _aws_attributes(message),
        }
        if self.queue_url.endswith(".fifo"):
            kwargs["MessageGroupId"] = message.subject
            kwargs["MessageDeduplicationId"] = message.event_id
        try:
            self.client.send_message(**kwargs)
        except self.errors as exc:
            raise aws_error(exc, "sqs", "SendMessage") from exc


class SnsSink:
    """``Publish`` to NOTIFICATION_SNS_TOPIC_ARN (FIFO topics as for SQS)."""

    def __init__(self, client: Any, topic_arn: str,
                 errors: tuple[type[BaseException], ...]) -> None:
        self.client = client
        self.topic_arn = topic_arn
        self.errors = errors

    def ping(self) -> None:
        try:
            self.client.get_topic_attributes(TopicArn=self.topic_arn)
        except self.errors as exc:
            raise aws_error(exc, "sns", "GetTopicAttributes") from exc

    def send(self, message: OutboundMessage) -> None:
        kwargs: dict[str, Any] = {
            "TopicArn": self.topic_arn,
            "Message": message.body.decode("utf-8"),
            "MessageAttributes": _aws_attributes(message),
        }
        if self.topic_arn.endswith(".fifo"):
            kwargs["MessageGroupId"] = message.subject
            kwargs["MessageDeduplicationId"] = message.event_id
        try:
            self.client.publish(**kwargs)
        except self.errors as exc:
            raise aws_error(exc, "sns", "Publish") from exc


def _aws_client(settings: Settings, service: str, adapter: str) -> tuple[Any, tuple[Any, ...]]:
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise _missing_sdk(adapter, "boto3", "aws") from None
    timeout = settings.notification_timeout_s
    client = boto3.client(
        service,
        region_name=settings.notification_aws_region,
        endpoint_url=settings.notification_aws_endpoint_url,
        config=Config(connect_timeout=timeout, read_timeout=timeout,
                      retries={"max_attempts": 1}),
    )
    return client, (BotoCoreError, ClientError)


def _sqs(settings: Settings) -> SqsSink:
    url = _required(settings.notification_sqs_queue_url, "notification_sqs_queue_url", "sqs")
    client, errors = _aws_client(settings, "sqs", "sqs")
    return SqsSink(client, url, errors)


def _sns(settings: Settings) -> SnsSink:
    arn = _required(settings.notification_sns_topic_arn, "notification_sns_topic_arn", "sns")
    client, errors = _aws_client(settings, "sns", "sns")
    return SnsSink(client, arn, errors)


# ---- Google Pub/Sub (REST) ----------------------------------------------------------------
class PubSubSink:
    """``topics.publish`` over REST: the envelope as data, the signature headers as
    attributes. ``token`` returns a bearer token, or None (an emulator)."""

    def __init__(self, endpoint: str, project: str, topic: str, client: httpx.Client,
                 token: Callable[[], str | None]) -> None:
        self.topic_url = (
            f"{endpoint.rstrip('/')}/v1/projects/{quote(project, safe='')}"
            f"/topics/{quote(topic, safe='')}"
        )
        self.client = client
        self._token = token

    def _headers(self) -> dict[str, str]:
        token = self._token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def ping(self) -> None:
        try:
            response = self.client.get(self.topic_url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise NotificationDeliveryError(
                f"pubsub unreachable: {type(exc).__name__}", retryable=True, sink="pubsub"
            ) from exc
        if response.status_code >= 300:
            raise NotificationDeliveryError(
                f"pubsub answered HTTP {response.status_code}",
                retryable=is_retryable_status(response.status_code),
                sink="pubsub", status_code=response.status_code,
            )

    def send(self, message: OutboundMessage) -> None:
        body = {
            "messages": [{
                "data": base64.b64encode(message.body).decode("ascii"),
                "attributes": {**message.headers, "event-type": message.event_type},
            }]
        }
        post(self.client, f"{self.topic_url}:publish", sink="pubsub",
             content=json.dumps(body).encode("utf-8"),
             headers={"Content-Type": "application/json", **self._headers()})


def _pubsub(settings: Settings) -> PubSubSink:
    project = _required(settings.notification_pubsub_project, "notification_pubsub_project",
                        "pubsub")
    topic = _required(settings.notification_pubsub_topic, "notification_pubsub_topic",
                      "pubsub")
    token: Callable[[], str | None]
    if settings.notification_pubsub_credentials == "adc":
        from oran_adapt.adapters.registry.vertex import AdcToken

        token = AdcToken()
    else:
        def token() -> str | None:
            return None
    return PubSubSink(settings.notification_pubsub_endpoint, project, topic,
                      http_client(settings), token)


# ---- NATS (core protocol over TCP) ----------------------------------------------------------
class NatsSink:
    """Publishes with ``HPUB`` (headers) on one short connection per message, then ``PING``
    and waits for ``PONG``: the server has processed the publish once it answers, and any
    ``-ERR`` (authorization, bad subject) is seen. ``nats://`` is plain TCP, ``tls://`` TLS
    with the system trust store."""

    def __init__(self, url: str, subject: str, token: str | None, timeout_s: float,
                 name: str = "oran-adapt") -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"nats", "tls"} or not parts.hostname:
            raise ConfigurationError(
                "NOTIFICATION_NATS_URL must be nats://host:port or tls://host:port",
                key="NOTIFICATION_NATS_URL",
            )
        self.host = parts.hostname
        self.port = parts.port or 4222
        self.tls = parts.scheme == "tls"
        self.subject = subject
        self._token = token
        self.timeout_s = timeout_s
        self.name = name

    def _fail(self, text: str, *, retryable: bool) -> NotificationDeliveryError:
        return NotificationDeliveryError(f"nats {text}", retryable=retryable, sink="nats")

    def _connect(self) -> tuple[socket.socket, Any]:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            reader = sock.makefile("rb")
            info_line = reader.readline()
        except (OSError, ssl.SSLError) as exc:
            raise self._fail(f"unreachable: {type(exc).__name__}", retryable=True) from exc
        if not info_line.startswith(b"INFO "):
            sock.close()
            raise self._fail("server did not greet with INFO", retryable=True)
        try:
            info = json.loads(info_line[5:])
        except ValueError:
            sock.close()
            raise self._fail("sent an unreadable INFO", retryable=True) from None
        if info.get("tls_required") and not self.tls:
            sock.close()
            raise self._fail("server requires TLS (use tls:// in NOTIFICATION_NATS_URL)",
                             retryable=False)
        connect: dict[str, Any] = {"verbose": False, "pedantic": False, "headers": True,
                                   "name": self.name, "lang": "python", "version": "1"}
        if self._token:
            connect["auth_token"] = self._token
        try:
            sock.sendall(b"CONNECT " + json.dumps(connect).encode() + b"\r\n")
        except OSError as exc:
            sock.close()
            raise self._fail(f"connect failed: {type(exc).__name__}", retryable=True) from exc
        return sock, reader

    def _await_pong(self, sock: socket.socket, reader: Any) -> None:
        try:
            sock.sendall(b"PING\r\n")
        except OSError as exc:
            raise self._fail(f"send failed: {type(exc).__name__}", retryable=True) from exc
        while True:
            try:
                line = reader.readline()
            except OSError as exc:
                raise self._fail(f"no answer: {type(exc).__name__}", retryable=True) from exc
            if not line:
                raise self._fail("closed the connection", retryable=True)
            if line.startswith(b"PONG"):
                return
            if line.startswith(b"-ERR"):
                text = line[4:].strip().decode("utf-8", "replace")
                auth = "authorization" in text.lower() or "permission" in text.lower()
                raise self._fail(f"refused: {text}", retryable=not auth)
            if line.startswith(b"PING"):
                sock.sendall(b"PONG\r\n")

    def ping(self) -> None:
        sock, reader = self._connect()
        try:
            self._await_pong(sock, reader)
        finally:
            sock.close()

    def send(self, message: OutboundMessage) -> None:
        subject = self.subject.format(event_type=message.event_type)
        if any(c.isspace() for c in subject):
            raise self._fail(f"subject {subject!r} contains whitespace", retryable=False)
        headers = {**message.headers, "Nats-Msg-Id": message.event_id,
                   "event-type": message.event_type}
        header_block = ("NATS/1.0\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode("utf-8")
        total = len(header_block) + len(message.body)
        frame = (f"HPUB {subject} {len(header_block)} {total}\r\n".encode()
                 + header_block + message.body + b"\r\n")
        sock, reader = self._connect()
        try:
            try:
                sock.sendall(frame)
            except OSError as exc:
                raise self._fail(f"send failed: {type(exc).__name__}", retryable=True) from exc
            self._await_pong(sock, reader)
        finally:
            sock.close()


def _nats(settings: Settings) -> NatsSink:
    url = _required(settings.notification_nats_url, "notification_nats_url", "nats")
    token = settings.notification_nats_token
    return NatsSink(url, settings.notification_nats_subject,
                    token.get_secret_value() if token is not None else None,
                    settings.notification_timeout_s)


KAFKA = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="kafka",
        description="one record per event on NOTIFICATION_KAFKA_TOPIC, keyed by job id",
        features=frozenset({"network", "broker", "envelope", "signed"}),
        config_keys=("kafka_bootstrap_servers", "notification_kafka_topic",
                     "notification_timeout_s"),
        required_keys=("kafka_bootstrap_servers", "notification_kafka_topic"),
        distributions=("confluent-kafka",),
    ),
    factory=_kafka,
)

SQS = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="sqs",
        description="one SQS message per event (FIFO: grouped by job, deduplicated by event)",
        features=frozenset({"network", "broker", "envelope", "signed"}),
        config_keys=("notification_sqs_queue_url", "notification_aws_region",
                     "notification_aws_endpoint_url", "notification_timeout_s"),
        required_keys=("notification_sqs_queue_url",),
        distributions=("boto3",),
    ),
    factory=_sqs,
)

SNS = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="sns",
        description="one SNS publish per event (FIFO: grouped by job, deduplicated by event)",
        features=frozenset({"network", "broker", "envelope", "signed"}),
        config_keys=("notification_sns_topic_arn", "notification_aws_region",
                     "notification_aws_endpoint_url", "notification_timeout_s"),
        required_keys=("notification_sns_topic_arn",),
        distributions=("boto3",),
    ),
    factory=_sns,
)

PUBSUB = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="pubsub",
        description="one Google Pub/Sub message per event (REST publish)",
        features=frozenset({"network", "broker", "envelope", "signed"}),
        config_keys=("notification_pubsub_project", "notification_pubsub_topic",
                     "notification_pubsub_endpoint", "notification_pubsub_credentials",
                     "notification_timeout_s"),
        required_keys=("notification_pubsub_project", "notification_pubsub_topic"),
    ),
    factory=_pubsub,
)

NATS = AdapterSpec(
    capability=Capability(
        port="notification",
        adapter="nats",
        description="one NATS message per event on NOTIFICATION_NATS_SUBJECT (with headers)",
        features=frozenset({"network", "broker", "envelope", "signed"}),
        config_keys=("notification_nats_url", "notification_nats_subject",
                     "notification_nats_token", "notification_timeout_s"),
        required_keys=("notification_nats_url",),
    ),
    factory=_nats,
)
