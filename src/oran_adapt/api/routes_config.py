"""Introspection: the installed adapters and the effective configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from oran_adapt import plugins
from oran_adapt.api.security import ADMIN, require
from oran_adapt.core.config import ADAPTER_SELECTORS
from oran_adapt.core.config_sources import effective_config

router = APIRouter(tags=["config"])


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    """Every port with its installed adapters' capability descriptors, and which one the
    configuration selected (null for a port no selector key chooses, or one switched off)."""
    settings = request.app.state.settings
    selected = {port: getattr(settings, key) for key, port in ADAPTER_SELECTORS.items()}
    return {
        "ports": {
            port: {
                "selected": selected.get(port)
                if any(c.adapter == selected.get(port) for c in caps)
                else None,
                "adapters": [c.as_dict() for c in caps],
            }
            for port, caps in plugins.capabilities().items()
        }
    }


@router.get("/config/effective", dependencies=[Depends(require(ADMIN))])
def config_effective(request: Request) -> dict[str, Any]:
    """Every configuration key's effective value and the layer it came from. Secrets are
    redacted, URL passwords and query strings masked, API key digests shortened."""
    return {"keys": effective_config(request.app.state.settings)}
