"""ASGI entrypoint: `uvicorn oran_adapt.api.main:app`."""

from oran_adapt.api.app import create_app

app = create_app()
