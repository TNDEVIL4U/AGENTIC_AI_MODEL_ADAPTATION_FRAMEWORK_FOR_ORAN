"""Receiving ends for the notification sinks, all local and in-process: an HTTP receiver
(webhook, Slack, PagerDuty, Pub/Sub REST) that can fail, stall or check signatures; a Kafka
producer; SQS/SNS clients; a NATS server speaking the core protocol over TCP; an SMTP server
object; and a log capture. Each one exposes ``payloads()`` (what it accepted, oldest first) and
``fail_next(retryable)`` (refuse the next message) for the conformance suite."""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from oran_adapt.core.errors import SignatureVerificationError
from oran_adapt.notifications.signing import verify


# ---- HTTP -----------------------------------------------------------------------------------
class Receiver:
    """A local HTTP endpoint. ``statuses`` are answered to the next requests in turn (then 200);
    ``delay_s`` stalls each POST; with ``keys`` every POST's signature is checked and a bad one
    answered 401 (and not accepted)."""

    def __init__(self, *, port: int = 0, keys: list[bytes] | None = None,
                 tolerance_s: float = 300.0,
                 decode: Callable[[bytes], bytes] = lambda body: body) -> None:
        self.keys = keys
        self.tolerance_s = tolerance_s
        self.decode = decode
        self.statuses: list[int] = []
        self.delay_s = 0.0
        # While set, each POST is held open until ``release()`` and then dropped unanswered:
        # its sender is gone by then (the crash test kills it mid-request).
        self.gate: threading.Event | None = None
        self.accepted: list[tuple[dict[str, str], bytes]] = []
        self.rejected_signatures = 0
        self.posts = 0
        self._lock = threading.Lock()
        self._held: list[threading.Event] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                return

            def _answer(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                self._answer(200)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = {k: v for k, v in self.headers.items()}
                with receiver._lock:
                    receiver.posts += 1
                    status = receiver.statuses.pop(0) if receiver.statuses else None
                    delay = receiver.delay_s
                    gate = receiver.gate
                    if gate is not None:
                        receiver._held.append(gate)
                if gate is not None:
                    gate.wait()
                    return
                if delay:
                    time.sleep(delay)
                if status is not None:
                    self._answer(status)
                    return
                if receiver.keys is not None:
                    try:
                        verify(body, headers, receiver.keys, tolerance_s=receiver.tolerance_s)
                    except SignatureVerificationError:
                        with receiver._lock:
                            receiver.rejected_signatures += 1
                        self._answer(401)
                        return
                with receiver._lock:
                    receiver.accepted.append((headers, body))
                self._answer(200)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                return  # a sender that timed out or died mid-request: expected here

        self.server = Server(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def start(self) -> Receiver:
        self._thread.start()
        return self

    def release(self) -> None:
        """Let every held POST go (unanswered)."""
        with self._lock:
            held, self._held = self._held, []
        for gate in held:
            gate.set()

    def stop(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()

    def payloads(self) -> list[bytes]:
        with self._lock:
            return [self.decode(body) for _, body in self.accepted]

    def fail_next(self, retryable: bool) -> None:
        with self._lock:
            self.statuses.append(503 if retryable else 400)


def pubsub_decode(body: bytes) -> bytes:
    return base64.b64decode(json.loads(body)["messages"][0]["data"])


def free_port() -> int:
    """A port nothing listens on (yet): a sink pointed at it is "down"."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---- Kafka ----------------------------------------------------------------------------------
class FakeKafkaException(Exception):
    pass


class _KafkaError:
    def __init__(self, retriable: bool) -> None:
        self._retriable = retriable

    def retriable(self) -> bool:
        return self._retriable

    def __str__(self) -> str:
        return "retriable broker error" if self._retriable else "topic authorization failed"


class FakeProducer:
    """confluent_kafka.Producer's produce / flush / list_topics, acknowledging on flush."""

    def __init__(self) -> None:
        self.records: list[tuple[str, bytes, bytes, list[tuple[str, bytes]]]] = []
        self._pending: list[tuple[str, bytes, bytes, Any, Any]] = []
        self._failures: list[bool] = []

    def list_topics(self, topic: str, timeout: float) -> None:
        return None

    def produce(self, topic: str, *, key: bytes, value: bytes, headers: Any,
                on_delivery: Any) -> None:
        self._pending.append((topic, key, value, headers, on_delivery))

    def flush(self, timeout: float) -> int:
        while self._pending:
            topic, key, value, headers, callback = self._pending.pop(0)
            if self._failures:
                callback(_KafkaError(self._failures.pop(0)), None)
                continue
            self.records.append((topic, key, value, headers))
            callback(None, None)
        return 0

    def payloads(self) -> list[bytes]:
        return [key + b"|" + value for _, key, value, _ in self.records]

    def fail_next(self, retryable: bool) -> None:
        self._failures.append(retryable)


# ---- AWS ------------------------------------------------------------------------------------
class FakeClientError(Exception):
    """botocore.exceptions.ClientError's shape: a ``response`` dict."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status},
                         "Error": {"Code": code}}


class FakeAws:
    """The SQS and SNS client calls the sinks make."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._failures: list[FakeClientError] = []

    def _call(self, kwargs: dict[str, Any]) -> dict[str, str]:
        if self._failures:
            raise self._failures.pop(0)
        self.calls.append(kwargs)
        return {"MessageId": str(len(self.calls))}

    def send_message(self, **kwargs: Any) -> dict[str, str]:
        return self._call(kwargs)

    def publish(self, **kwargs: Any) -> dict[str, str]:
        return self._call(kwargs)

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {"Attributes": {"QueueArn": "arn:aws:sqs:local:0:q"}}

    def get_topic_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {"Attributes": {}}

    def payloads(self) -> list[bytes]:
        return [(c.get("MessageBody") or c["Message"]).encode("utf-8") for c in self.calls]

    def fail_next(self, retryable: bool) -> None:
        self._failures.append(
            FakeClientError(503, "ServiceUnavailable") if retryable
            else FakeClientError(403, "AccessDenied")
        )


# ---- NATS -----------------------------------------------------------------------------------
class NatsServer:
    """Enough of a NATS server for the sink: INFO, CONNECT, HPUB, PING/PONG, -ERR. A retryable
    failure drops the connection unanswered; a permanent one is a permissions -ERR."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.messages: list[tuple[str, bytes, bytes]] = []
        self.connects: list[dict[str, Any]] = []
        self._failures: list[bool] = []
        self._lock = threading.Lock()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self._sock.getsockname()[1]}"

    def start(self) -> NatsServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        self._sock.close()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as reader:
            conn.sendall(b'INFO {"server_id":"fake","headers":true,"max_payload":1048576}\r\n')
            pending: tuple[str, bytes, bytes] | None = None
            while True:
                line = reader.readline()
                if not line:
                    return
                if line.startswith(b"CONNECT "):
                    options = json.loads(line[8:])
                    with self._lock:
                        self.connects.append(options)
                    if self.token is not None and options.get("auth_token") != self.token:
                        self._refuse(conn, reader, b"-ERR 'Authorization Violation'\r\n")
                        return
                elif line.startswith(b"HPUB "):
                    _, subject, hlen, total = line.split()
                    frame = reader.read(int(total) + 2)
                    headers, body = frame[: int(hlen)], frame[int(hlen): int(total)]
                    pending = (subject.decode(), headers, body)
                elif line.startswith(b"PING"):
                    with self._lock:
                        failure = self._failures.pop(0) if self._failures else None
                        if failure is None and pending is not None:
                            self.messages.append(pending)
                    if failure is True:
                        return  # dropped: the sink sees the connection close
                    if failure is False:
                        self._refuse(conn, reader,
                                     b"-ERR 'Permissions Violation for Publish'\r\n")
                        return
                    conn.sendall(b"PONG\r\n")
                    pending = None

    @staticmethod
    def _refuse(conn: socket.socket, reader: Any, error: bytes) -> None:
        """Send ``-ERR`` and close gracefully, as nats-server does: closing with unread input
        would reset the connection (on Windows) and discard the error before it is read."""
        conn.sendall(error)
        conn.shutdown(socket.SHUT_WR)
        conn.settimeout(5.0)
        try:
            while reader.read1(4096):
                continue
        except OSError:
            return

    def payloads(self) -> list[bytes]:
        with self._lock:
            return [body for _, _, body in self.messages]

    def fail_next(self, retryable: bool) -> None:
        with self._lock:
            self._failures.append(retryable)


# ---- SMTP -----------------------------------------------------------------------------------
class SmtpServer:
    """Stands in for ``smtplib.SMTP``: call it like the class to open a session."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.logins: list[tuple[str, str]] = []
        self._failures: list[bool] = []

    def __call__(self, host: str, port: int, timeout: float) -> _SmtpSession:
        return _SmtpSession(self)

    def payloads(self) -> list[bytes]:
        return [mail.get_content().encode("utf-8") for mail in self.sent]

    def fail_next(self, retryable: bool) -> None:
        self._failures.append(retryable)


class _SmtpSession:
    def __init__(self, server: SmtpServer) -> None:
        self.server = server

    def starttls(self, context: Any) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.server.logins.append((user, password))

    def noop(self) -> tuple[int, bytes]:
        return 250, b"OK"

    def send_message(self, mail: Any, from_addr: str, to_addrs: list[str]) -> None:
        if self.server._failures:
            retryable = self.server._failures.pop(0)
            code = 451 if retryable else 550
            raise smtplib.SMTPResponseException(code, b"refused by the test server")
        self.server.sent.append(mail)

    def quit(self) -> None:
        return None

    def close(self) -> None:
        return None


# ---- log ------------------------------------------------------------------------------------
class LogCapture(logging.Handler):
    """The ``log`` sink's lines."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "sink", None) == "log" and record.levelno >= logging.INFO:
            self.lines.append(record.getMessage())

    def payloads(self) -> list[bytes]:
        return [line.encode("utf-8") for line in self.lines]
