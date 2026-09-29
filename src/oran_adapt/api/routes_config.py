"""Introspection: the installed adapters and the effective configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from oran_adapt import plugins
from oran_adapt.api.security import ADMIN, require
from oran_adapt.core.config import ADAPTER_SELECTORS, MULTI_SELECTORS, selected_adapters
from oran_adapt.core.config_sources import effective_config

router = APIRouter(tags=["config"])


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    """Every port with its installed adapters' capability descriptors, and which one the
    configuration selected (null for a port no selector key chooses, or one switched off). A
    port that takes several adapters at once (notification sinks) lists every one selected."""
    settings = request.app.state.settings
    selected = {
        port: selected_adapters(settings, key) for key, port in ADAPTER_SELECTORS.items()
    }
    ports: dict[str, Any] = {}
    for port, caps in plugins.capabilities().items():
        installed = {c.adapter for c in caps}
        names = [n for n in selected.get(port, []) if n in installed]
        multi = any(ADAPTER_SELECTORS[k] == port for k in MULTI_SELECTORS)
        ports[port] = {
            "selected": names if multi else (names[0] if names else None),
            "adapters": [c.as_dict() for c in caps],
        }
    return {"ports": ports}


@router.get("/config/effective", dependencies=[Depends(require(ADMIN))])
def config_effective(request: Request) -> dict[str, Any]:
    """Every configuration key's effective value and the layer it came from. Secrets are
    redacted, URL passwords and query strings masked, API key digests shortened."""
    return {"keys": effective_config(request.app.state.settings)}
