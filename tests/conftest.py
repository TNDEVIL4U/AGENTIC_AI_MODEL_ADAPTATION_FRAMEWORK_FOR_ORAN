"""Shared fixtures. Real SQLite DB + real file/SQLite-backed MLflow in temp dirs (no mocks)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.db.migrate import upgrade_to_head


@pytest.fixture(autouse=True)
def _isolate_mlflow_global_uris(monkeypatch) -> Iterator[None]:
    """Several tests point MLflow's process-global tracking/registry URIs (and the MLFLOW_*_URI
    env vars the setters write) at their own temp store. Reset both after every test so a later
    test's fluent ``log_model`` can't silently register into an earlier test's store."""
    from mlflow.tracking import _model_registry, _tracking_service

    for key in ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI"):
        monkeypatch.delenv(key, raising=False)
    yield
    _tracking_service.utils._tracking_uri = None
    _model_registry.utils._registry_uri = None
    for key in ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI"):
        os.environ.pop(key, None)  # monkeypatch then restores any value the shell had set


@pytest.fixture
def database_url(tmp_path) -> str:
    # Set TEST_DATABASE_URL to a PostgreSQL URL to run the suite against real PostgreSQL.
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'app.db').as_posix()}"


@pytest.fixture
def settings(tmp_path, database_url) -> Settings:
    art = tmp_path / "mlartifacts"
    art.mkdir()
    return Settings(
        _env_file=None,
        database_url=database_url,
        mlflow_tracking_uri=f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp_path / "work"),
        log_json=False,
    )


@pytest.fixture
def migrated_settings(settings) -> Settings:
    upgrade_to_head(settings.database_url)
    return settings


@pytest.fixture
def client(migrated_settings) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_settings)) as c:
        yield c
