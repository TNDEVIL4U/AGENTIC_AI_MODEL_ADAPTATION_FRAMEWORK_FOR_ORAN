"""A local model-serving stub: a real HTTP server on 127.0.0.1 (stdlib, no dependencies) that
the ``webhook``, ``bentoml`` and ``triton`` deployment adapters talk to over the network.

It implements
- the webhook deployment contract (docs/adapters/deployment.md): ``GET /health``,
  ``GET /status?model=``, ``POST /deploy``; also under the ``/oran`` prefix the BentoML
  service template exposes. A deploy is accepted at once and settles after a few status reads;
- the KServe v2 / Triton model-repository extension in explicit model-control mode:
  ``GET /v2/health/live``, ``POST /v2/repository/index``,
  ``POST /v2/repository/models/<name>/load`` (reads ``<repository>/<name>/config.pbtxt`` and
  the staged version directory, like Triton) and ``.../unload``.

``fail_next(model)`` makes the next deploy (webhook: reported as failed) or load (Triton: HTTP
400, the loaded version keeps serving) of ``model`` fail. It is a stub, not Triton or BentoML:
results against it are "unverified against real systems".
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

SETTLE_AFTER = 2
_VERSIONS = re.compile(r"versions:\s*\[\s*(\d+)\s*\]")


class ServingStub:
    def __init__(self, repository: str | None = None, token: str | None = None) -> None:
        self.repository = Path(repository) if repository else None
        self.token = token
        self.lock = threading.Lock()
        self.deployed: dict[str, dict[str, Any]] = {}  # webhook: model -> state
        self.triton: dict[str, dict[str, str]] = {}  # name -> {version: state}
        self.failing: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _reply(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _dispatch(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if stub.token and self.headers.get("Authorization") != f"Bearer {stub.token}":
                    self._reply(401, {"error": "unauthorized"})
                    return
                status, reply = stub.route(self.command, self.path, body)
                self._reply(status, reply)

            do_GET = _dispatch
            do_POST = _dispatch

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def fail_next(self, model: str) -> None:
        with self.lock:
            self.failing.add(model)

    # -- routing --

    def route(self, method: str, raw_path: str, body: dict[str, Any]) -> tuple[int, Any]:
        parts = urlsplit(raw_path)
        path = parts.path.removeprefix("/oran")
        query = parse_qs(parts.query)
        with self.lock:
            self.requests.append((method, path))
            if path.startswith("/v2/"):
                return self._triton_route(method, path)
            if method == "GET" and path == "/health":
                return 200, {"status": "ok"}
            if method == "GET" and path == "/status":
                return self._status(query.get("model", [""])[0])
            if method == "POST" and path == "/deploy":
                return self._deploy(body)
        return 404, {"error": "not found"}

    # -- webhook contract --

    def _status(self, model: str) -> tuple[int, Any]:
        state = self.deployed.get(model)
        if state is None:
            return 404, {"error": f"model {model} is not deployed"}
        if not state["ready"] and not state["failed"]:
            state["polls"] -= 1
            if state["polls"] <= 0:
                if state["fail"]:
                    state.update(failed=True, detail="the model server crashed on load",
                                 version=state["version"])
                else:
                    state["ready"] = True
        if state["version"] is None and state["ready"]:
            del self.deployed[model]
            return 404, {"error": f"model {model} is not deployed"}
        return 200, {k: state[k] for k in ("version", "ready", "failed", "detail")}

    def _deploy(self, body: dict[str, Any]) -> tuple[int, Any]:
        model = body.get("model")
        if not isinstance(model, str) or "version" not in body:
            return 400, {"error": "model and version are required"}
        fail = model in self.failing
        self.failing.discard(model)
        self.deployed[model] = {
            "version": body["version"], "source": body.get("source"), "ready": False,
            "failed": False, "detail": "rolling out", "polls": SETTLE_AFTER, "fail": fail,
        }
        return 202, {"accepted": True}

    # -- Triton repository API --

    def _triton_route(self, method: str, path: str) -> tuple[int, Any]:
        if method == "GET" and path == "/v2/health/live":
            return 200, {}
        if method == "POST" and path == "/v2/repository/index":
            return 200, [
                {"name": name, "version": version, "state": state,
                 **({"reason": "unloaded"} if state == "UNAVAILABLE" else {})}
                for name, versions in sorted(self.triton.items())
                for version, state in sorted(versions.items())
            ]
        match = re.fullmatch(r"/v2/repository/models/([^/]+)/(load|unload)", path)
        if method == "POST" and match:
            name, action = match.groups()
            if action == "unload":
                for version in self.triton.get(name, {}):
                    self.triton[name][version] = "UNAVAILABLE"
                return 200, {}
            return self._load(name)
        return 404, {"error": "not found"}

    def _load(self, name: str) -> tuple[int, Any]:
        if self.repository is None:
            return 400, {"error": "no model repository"}
        config = self.repository / name / "config.pbtxt"
        if not config.is_file():
            return 400, {"error": f"failed to load '{name}': no config.pbtxt"}
        found = _VERSIONS.search(config.read_text(encoding="utf-8"))
        if found is None:
            return 400, {"error": f"failed to load '{name}': no version policy"}
        version = found.group(1)
        if not (self.repository / name / version).is_dir():
            return 400, {"error": f"failed to load '{name}': version {version} not staged"}
        if name in self.failing:
            self.failing.discard(name)
            return 400, {"error": f"failed to load '{name}' version {version}: model crashed"}
        self.triton[name] = {v: "UNAVAILABLE" for v in self.triton.get(name, {})}
        self.triton[name][version] = "READY"
        return 200, {}
