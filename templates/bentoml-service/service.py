"""A BentoML service that the ``bentoml`` deployment adapter can drive.

It serves the framework's deployment contract under ``/oran`` (docs/adapters/deployment.md):

- ``GET  /oran/health``             -> 200
- ``GET  /oran/status?model=<m>``   -> {"version", "ready", "failed", "detail"}, 404 if not deployed
- ``POST /oran/deploy``             -> 202 once accepted; the model loads in the background

``status`` reports what this process actually has loaded, never what was merely requested, so
the framework's post-deploy read-back means something. A load that raises is reported as
``failed`` and the previously loaded version keeps serving until the framework restores it.

UNVERIFIED: written against the BentoML >= 1.2 service API, but never run against a real
BentoML install in this project (only the contract is tested, with a stdlib stub). Replace
``load_model`` and ``predict`` with your model's code.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import bentoml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

TOKEN = os.environ.get("ORAN_DEPLOY_TOKEN")  # the framework's BENTOML_TOKEN, if set

oran = FastAPI()
_lock = threading.Lock()
_serving: dict[str, tuple[str, Any]] = {}  # model -> (version, loaded model)
_pending: dict[str, dict[str, Any]] = {}  # model -> {"version", "failed", "detail"}


def load_model(model: str, version: str, source: str | None) -> Any:
    """Load one model version. ``source`` is the registry version's artifact location.

    Replace this: e.g. download ``source`` and open it with onnxruntime. Raise on failure.
    """
    if source is None:
        raise RuntimeError(f"no artifact location for {model} version {version}")
    return {"model": model, "version": version, "source": source}


def _check_token(request: Request) -> None:
    if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


def _load(model: str, version: str, source: str | None) -> None:
    try:
        loaded = load_model(model, version, source)
    except Exception as exc:  # reported through /status, never swallowed
        with _lock:
            _pending[model] = {"version": version, "failed": True, "detail": str(exc)[:500]}
        return
    with _lock:
        _serving[model] = (version, loaded)
        _pending.pop(model, None)


@oran.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@oran.get("/status")
def status(model: str, request: Request) -> JSONResponse:
    _check_token(request)
    with _lock:
        pending = _pending.get(model)
        serving = _serving.get(model)
    if pending is not None:
        return JSONResponse({"version": pending["version"], "ready": False,
                             "failed": pending["failed"], "detail": pending["detail"]})
    if serving is None:
        return JSONResponse({"detail": "not deployed"}, status_code=404)
    return JSONResponse({"version": serving[0], "ready": True, "failed": False, "detail": ""})


@oran.post("/deploy")
async def deploy(request: Request) -> JSONResponse:
    _check_token(request)
    body = await request.json()
    model, version, source = body.get("model"), body.get("version"), body.get("source")
    if not isinstance(model, str) or not model:
        return JSONResponse({"detail": "model is required"}, status_code=400)
    if version is None:  # undeploy
        with _lock:
            _serving.pop(model, None)
            _pending.pop(model, None)
        return JSONResponse({"accepted": True}, status_code=202)
    with _lock:
        _pending[model] = {"version": str(version), "failed": False, "detail": "loading"}
    threading.Thread(target=_load, args=(model, str(version), source), daemon=True).start()
    return JSONResponse({"accepted": True}, status_code=202)


@bentoml.service
@bentoml.asgi_app(oran, path="/oran")
class OranModels:
    @bentoml.api
    def predict(self, model: str, inputs: list[list[float]]) -> dict[str, Any]:
        with _lock:
            serving = _serving.get(model)
        if serving is None:
            raise bentoml.exceptions.NotFound(f"model {model!r} is not deployed")
        version, _loaded = serving
        # Replace with the real inference call on ``_loaded``.
        return {"model": model, "version": version, "outputs": inputs}
