"""Local HTTP doubles of the LLM providers' wire formats, for the Hardening Phase 10 tests and
the LLM conformance suite: an OpenAI-compatible ``/chat/completions``, Anthropic's
``/v1/messages`` and Gemini's ``:generateContent``. Each serves on 127.0.0.1 and records the
requests it received. Nothing here reaches the internet."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPLY = "Hello from the local double."
INPUT_TOKENS, OUTPUT_TOKENS = 17, 5


def _openai(body: dict[str, Any], reply: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-local", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
        "usage": {"prompt_tokens": INPUT_TOKENS, "completion_tokens": OUTPUT_TOKENS,
                  "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS},
    }


def _anthropic(body: dict[str, Any], reply: str) -> dict[str, Any]:
    return {
        "id": "msg_local", "type": "message", "role": "assistant", "model": body.get("model"),
        "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
    }


def _gemini(body: dict[str, Any], reply: str) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": reply}]},
                        "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": INPUT_TOKENS,
                          "candidatesTokenCount": OUTPUT_TOKENS,
                          "totalTokenCount": INPUT_TOKENS + OUTPUT_TOKENS},
        "modelVersion": "local",
    }


def _template(body: dict[str, Any], reply: str) -> dict[str, Any]:
    """templates/llm-adapter's format."""
    return {"text": reply, "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS}}


FORMATS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    "openai-compatible": _openai, "anthropic": _anthropic, "gemini": _gemini,
    "template": _template,
}


@dataclass
class LocalLlm:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    """Each request: path, headers (lower-case names) and JSON body."""
    replies: list[str] = field(default_factory=list)
    """Queued replies, used in order; REPLY once they run out."""
    status: int = 200


@contextmanager
def local_llm(kind: str) -> Iterator[LocalLlm]:
    render = FORMATS[kind]
    state = LocalLlm(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append({"path": self.path, "body": body,
                                   "headers": {k.lower(): v for k, v in self.headers.items()}})
            reply = state.replies.pop(0) if state.replies else REPLY
            payload = (json.dumps(render(body, reply)) if state.status < 400
                       else json.dumps({"error": {"message": "refused by the double"}}))
            data = payload.encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def closed_port_url() -> str:
    """An http URL on 127.0.0.1 whose port nothing listens on (connections are refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"
