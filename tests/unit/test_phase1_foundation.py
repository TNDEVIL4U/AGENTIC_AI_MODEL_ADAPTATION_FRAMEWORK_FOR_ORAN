"""Phase 1: foundation — config, logging, DB, migrations, MLflow, health endpoints."""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import (
    DatabaseUnavailableError,
    ModelNotFoundError,
    RegistryUnavailableError,
)
from oran_adapt.core.logging import JsonFormatter, log_event
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine
from oran_adapt.db.health import check_database
from oran_adapt.db.migrate import downgrade_to_base, upgrade_to_head
from oran_adapt.registry.client import MlflowRegistry

EXPECTED_TABLES = {
    "model_metadata", "dataset_metadata", "data_version", "data_record",
    "model_data_association", "performance_record", "adaptation_job",
    "adaptation_event", "audit_log",
}


# ---- application ---------------------------------------------------------------------------
def test_application_starts_and_liveness_ok(client) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_ok_when_db_and_mlflow_reachable(client) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert {c["name"] for c in body["components"]} == {"database", "mlflow"}


def test_ready_reports_503_when_database_down(migrated_settings) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    app = create_app(migrated_settings)
    # Swap in an engine pointing at a closed port: a genuinely unreachable PostgreSQL.
    app.state.engine = create_engine(
        "postgresql+psycopg://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    with TestClient(app) as c:
        r = c.get("/api/v1/ready")
    assert r.status_code == 503
    comps = {x["name"]: x for x in r.json()["components"]}
    assert comps["database"]["ok"] is False
    assert comps["mlflow"]["ok"] is True


# ---- database ------------------------------------------------------------------------------
def test_database_connects(settings) -> None:
    check_database(create_db_engine(settings.database_url))


def test_database_unreachable_raises_structured_error() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/none",
                           connect_args={"connect_timeout": 1})
    with pytest.raises(DatabaseUnavailableError) as ei:
        check_database(engine)
    assert ei.value.code == "DATABASE_UNAVAILABLE"


def test_migration_up_and_down(settings) -> None:
    upgrade_to_head(settings.database_url)
    engine = create_db_engine(settings.database_url)
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    downgrade_to_base(settings.database_url)
    assert not (EXPECTED_TABLES & set(inspect(create_db_engine(settings.database_url))
                                      .get_table_names()))


def test_no_model_version_table_in_postgres_schema(settings) -> None:
    """MLflow is the only model-version authority."""
    upgrade_to_head(settings.database_url)
    names = set(inspect(create_db_engine(settings.database_url)).get_table_names())
    assert not {n for n in names if "model_version" in n or n == "model_versions"}


# ---- MLflow --------------------------------------------------------------------------------
def test_mlflow_connects(settings) -> None:
    MlflowRegistry(settings.mlflow_tracking_uri).ping()


def test_mlflow_unavailable_raises(tmp_path) -> None:
    reg = MlflowRegistry("http://127.0.0.1:1")
    with pytest.raises(RegistryUnavailableError):
        reg.ping()


def test_missing_model_raises_not_found(settings) -> None:
    reg = MlflowRegistry(settings.mlflow_tracking_uri)
    with pytest.raises(ModelNotFoundError):
        reg.list_versions("does_not_exist")


# ---- config / logging / schemas ------------------------------------------------------------
def test_settings_reject_provider_without_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="anthropic")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="gemini")
    assert Settings(_env_file=None, llm_provider="none").llm_provider == "none"


def test_structured_log_contains_context_fields() -> None:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    rec.adaptation_job_id = "job1"
    rec.component = "member1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["adaptation_job_id"] == "job1" and out["component"] == "member1"
    assert out["message"] == "hello" and "timestamp" in out


def test_log_event_only_passes_known_fields(caplog) -> None:
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("x"), "m", model_id="a", bogus="z")
    assert caplog.records[-1].model_id == "a"
    assert not hasattr(caplog.records[-1], "bogus")


def test_drift_event_idempotency_key_is_stable() -> None:
    a = DriftEvent(model_id="m", drift_detected=True, drift_score=0.8)
    b = DriftEvent(model_id="m", drift_detected=True, drift_score=0.8)
    c = DriftEvent(model_id="m", drift_detected=True, drift_score=0.9)
    assert a.idempotency_key() == b.idempotency_key() != c.idempotency_key()
    assert DriftEvent(model_id="m", drift_detected=True, event_id="e1").idempotency_key() == "m:e1"


def test_drift_event_validates_score_range() -> None:
    with pytest.raises(ValidationError):
        DriftEvent(model_id="m", drift_detected=True, drift_score=1.5)
