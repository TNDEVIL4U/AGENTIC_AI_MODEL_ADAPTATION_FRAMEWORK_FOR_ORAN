"""Phase 14 (Stage B): API keys with roles, a correlation id on every request, job and audit
row, Prometheus metrics under the spec's names, and an append-only audit trail that records
each step of a job (spec §40).

Everything runs against a real SQLite database and a real SQLite-backed MLflow in tmp_path.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_phase14_member1 import (  # shared seeding helpers (same test directory)
    _fit,
    _load_regime,
    _rsrp_regime,
    _seed,
    _seed_three_similar,
    _submit,
)

from oran_adapt import cli
from oran_adapt.api.app import create_app
from oran_adapt.api.security import hash_api_key
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import AuditAction, JobStatus
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import AdaptationJob, AuditLog
from oran_adapt.registry.client import MlflowRegistry

KEYS = {
    "admin": ("admin-key-0123456789", "ADMIN:alice"),
    "operator": ("operator-key-0123456789", "OPERATOR:noc"),
    "engineer": ("engineer-key-0123456789", "ML_ENGINEER:bob"),
    "reader": ("reader-key-0123456789", "READ_ONLY"),
}


def _h(who: str) -> dict[str, str]:
    return {"X-API-Key": KEYS[who][0]}


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


@pytest.fixture
def secured_settings(migrated_settings) -> Settings:
    return migrated_settings.model_copy(
        update={
            "auth_enabled": True,
            "api_keys": {hash_api_key(key): spec for key, spec in KEYS.values()},
        }
    )


@pytest.fixture
def secured(secured_settings):
    with TestClient(create_app(secured_settings)) as c:
        yield c


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _actions(session_factory, job_id: str) -> list[str]:
    with session_scope(session_factory) as session:
        rows = session.query(AuditLog).filter_by(job_id=job_id).order_by(AuditLog.id).all()
        return [r.action for r in rows]


# ---- authentication and roles ----------------------------------------------------------------
def test_protected_endpoints_need_a_key_and_the_right_role(secured) -> None:
    assert secured.get("/api/v1/health").status_code == 200  # open
    r = secured.get("/api/v1/models")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHENTICATED"
    assert secured.get("/api/v1/models", headers={"X-API-Key": "wrong-key-000000"}).status_code == 401

    for who in KEYS:
        assert secured.get("/api/v1/models", headers=_h(who)).status_code == 200, who
    bearer = {"Authorization": f"Bearer {KEYS['reader'][0]}"}
    assert secured.get("/api/v1/datasets", headers=bearer).status_code == 200

    # READ_ONLY may not write; OPERATOR may submit but not create data.
    event = {"model_id": "nope", "event_id": "e1"}
    denied = secured.post("/api/v1/adaptation/events", json=event, headers=_h("reader"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"
    ds = {"dataset_id": "kpi"}
    assert secured.post("/api/v1/datasets", json=ds, headers=_h("operator")).status_code == 403
    assert secured.post("/api/v1/datasets", json=ds, headers=_h("engineer")).status_code == 201

    # Rollback is for ADMIN and OPERATOR only; ML_ENGINEER is refused before anything happens.
    rb = secured.post("/api/v1/models/nope/rollback", json={}, headers=_h("engineer"))
    assert rb.status_code == 403
    assert secured.post("/api/v1/models/nope/rollback", json={}, headers=_h("operator")).status_code == 404


def test_auth_fails_closed_and_rejects_malformed_key_config(migrated_settings) -> None:
    closed = migrated_settings.model_copy(update={"auth_enabled": True, "api_keys": {}})
    with TestClient(create_app(closed)) as c:
        assert c.get("/api/v1/models", headers={"X-API-Key": "anything-at-all"}).status_code == 401
        assert c.get("/api/v1/readiness").status_code in (200, 503)  # never asks for a key
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_keys={"not-a-digest": "ADMIN"})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_keys={hash_api_key("k" * 20): "SUPERUSER"})


def test_cli_makes_a_key_whose_digest_grants_the_role(migrated_settings, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: migrated_settings)
    assert cli.main(["auth", "new-key", "--role", "OPERATOR", "--name", "noc"]) == 0
    out = json.loads(capsys.readouterr().out)
    [(digest, spec)] = out["api_keys_entry"].items()
    assert digest == hash_api_key(out["api_key"])
    assert spec == "OPERATOR:noc"
    Settings(_env_file=None, api_keys=out["api_keys_entry"])  # accepted as configuration


# ---- correlation id ---------------------------------------------------------------------------
def test_correlation_id_is_echoed_or_generated(client) -> None:
    r = client.get("/api/v1/health", headers={"X-Correlation-ID": "team1-req-42"})
    assert r.headers["X-Correlation-ID"] == "team1-req-42"
    fresh = client.get("/api/v1/health").headers["X-Correlation-ID"]
    assert len(fresh) == 32
    unsafe = client.get("/api/v1/health", headers={"X-Correlation-ID": "bad id\twith spaces"})
    assert unsafe.headers["X-Correlation-ID"] not in ("bad id\twith spaces", fresh)
    assert len(unsafe.headers["X-Correlation-ID"]) == 32


# ---- metrics ----------------------------------------------------------------------------------
SPEC_METRICS = [
    "adaptation_jobs_total", "adaptation_success_total", "adaptation_failure_total",
    "model_reuse_total", "fine_tune_total", "retrain_total", "rollback_total",
    "validation_failure_total", "adaptation_duration_seconds",
    "model_evaluation_duration_seconds", "cdc_events_total", "cdc_processing_lag",
    "llm_requests_total", "llm_failures_total", "sandbox_failures_total",
]


def test_metrics_endpoint_exposes_the_spec_metrics(client, migrated_settings) -> None:
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    for name in SPEC_METRICS:
        assert f"# TYPE {name.removesuffix('_total')}" in r.text, name

    private = migrated_settings.model_copy(
        update={"auth_enabled": True, "metrics_public": False,
                "api_keys": {hash_api_key(KEYS["reader"][0]): KEYS["reader"][1]}}
    )
    with TestClient(create_app(private)) as c:
        assert c.get("/api/v1/metrics").status_code == 401
        assert c.get("/api/v1/metrics", headers=_h("reader")).status_code == 200


# ---- audit trail: reuse path ------------------------------------------------------------------
def test_reuse_job_writes_the_spec_audit_sequence(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    signal_world = _rsrp_regime(120, seed=10)
    load_world = _load_regime(120, seed=20)
    _seed(
        session_factory, registry, migrated_settings, model_id="cell-a", name="cell_a",
        models=[_fit(signal_world), _fit(load_world), _fit(_rsrp_regime(120, seed=12))],
        live="3", historical=signal_world, drifted=_load_regime(100, seed=21),
    )
    reused_before = _sample("model_reuse_total")
    ok_before = _sample("adaptation_success_total", {"outcome": "REUSED"})
    evals_before = _sample("model_evaluation_duration_seconds_count")

    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-a", event_id="evt-audit-reuse", model_version="3"), tmp_path,
    )
    assert job.status == JobStatus.COMPLETED, job.error
    assert job.result["outcome"] == "REUSED"

    assert _actions(session_factory, job.job_id) == [
        "DRIFT_RECEIVED",
        "CURRENT_DATA_CREATED",
        "MODEL_VERSION_EVALUATED",
        "MODEL_VERSION_EVALUATED",
        "MODEL_VERSION_EVALUATED",
        "MODEL_REUSE_SELECTED",
        "MODEL_PROMOTED",
    ]
    with session_scope(session_factory) as session:
        chosen = session.query(AuditLog).filter_by(
            job_id=job.job_id, action="MODEL_REUSE_SELECTED"
        ).one()
        assert (chosen.model_version, chosen.decision) == ("2", "REUSE_EXISTING_VERSION")
        assert chosen.reason
        promoted = session.query(AuditLog).filter_by(
            job_id=job.job_id, action="MODEL_PROMOTED"
        ).one()
        assert (promoted.decision, promoted.detail["from_version"]) == ("REUSE", "3")
        assert promoted.created_at is not None and promoted.actor == "system"

    assert _sample("model_reuse_total") == reused_before + 1
    assert _sample("adaptation_success_total", {"outcome": "REUSED"}) == ok_before + 1
    assert _sample("model_evaluation_duration_seconds_count") == evals_before + 1


# ---- audit trail + auth + correlation: retrain through the API, then rollback -----------------
def test_retrain_via_api_is_audited_with_actor_and_correlation_id(
    session_factory, registry, secured_settings, secured
) -> None:
    _seed_three_similar(
        session_factory, registry, secured_settings, model_id="cell-b", name="cell_b"
    )
    retrain_before = _sample("retrain_total")
    jobs_before = _sample("adaptation_jobs_total", {"status": "COMPLETED", "outcome": "REGISTERED"})
    rollback_before = _sample("rollback_total", {"trigger": "manual"})

    r = secured.post(
        "/api/v1/adaptation/events",
        json={"model_id": "cell-b", "event_id": "evt-audit-retrain"},
        headers={**_h("operator"), "X-Correlation-ID": "drift-cell-b-001"},
    )
    assert r.status_code == 201, r.text
    assert r.headers["X-Correlation-ID"] == "drift-cell-b-001"
    job = r.json()
    assert job["status"] == "COMPLETED", job["error"]
    assert job["result"]["outcome"] == "REGISTERED"

    actions = _actions(session_factory, job["job_id"])
    assert actions[:2] == ["DRIFT_RECEIVED", "CURRENT_DATA_CREATED"]
    assert actions.count("MODEL_VERSION_EVALUATED") == 3
    tail = [a for a in actions if a != "MODEL_VERSION_EVALUATED"][2:]
    assert tail[0] == "ADAPTATION_DECISION_CREATED"
    assert tail[1] in ("RETRAIN_STARTED", "FINE_TUNE_STARTED")
    assert tail[2:] == [
        "VALIDATION_STARTED", "VALIDATION_PASSED", "MODEL_REGISTERED", "MODEL_PROMOTED",
    ]

    with session_scope(session_factory) as session:
        row = session.query(AdaptationJob).filter_by(job_id=job["job_id"]).one()
        assert row.correlation_id == "drift-cell-b-001"
        rows = session.query(AuditLog).filter_by(job_id=job["job_id"]).all()
        assert {a.correlation_id for a in rows} == {"drift-cell-b-001"}
        received = next(a for a in rows if a.action == "DRIFT_RECEIVED")
        assert received.actor == "noc"
        # The training-data snapshot is versioned, and audited, inside the same request.
        snap = session.query(AuditLog).filter_by(
            action="DATA_VERSION_CREATED", model_id="cell-b"
        ).one()
        assert snap.correlation_id == "drift-cell-b-001"
        assert len(snap.detail["content_hash"]) == 64
    if tail[1] == "RETRAIN_STARTED":
        assert _sample("retrain_total") == retrain_before + 1
    assert _sample(
        "adaptation_jobs_total", {"status": "COMPLETED", "outcome": "REGISTERED"}
    ) == jobs_before + 1

    # The operator rolls back; the audit row names them.
    rb = secured.post(
        "/api/v1/models/cell-b/rollback", json={"reason": "bad KPIs"}, headers=_h("operator")
    )
    assert rb.status_code == 200, rb.text
    with session_scope(session_factory) as session:
        rolled = session.query(AuditLog).filter_by(
            model_id="cell-b", action="MODEL_ROLLED_BACK"
        ).one()
        assert (rolled.actor, rolled.model_version, rolled.reason) == ("noc", "3", "bad KPIs")
    assert _sample("rollback_total", {"trigger": "manual"}) == rollback_before + 1


# ---- the audit log cannot be changed ----------------------------------------------------------
def test_audit_log_is_append_only(session_factory, migrated_settings) -> None:
    from oran_adapt.core.audit import record_audit

    with session_scope(session_factory) as session:
        row = record_audit(
            session, AuditAction.MODEL_ROLLED_BACK, component="test", model_id="m", reason="x"
        )
        session.flush()
        row_id = row.id

    with session_scope(session_factory) as session:
        row = session.get(AuditLog, row_id)
        row.reason = "rewritten"
        with pytest.raises(PermissionError):
            session.flush()
        session.rollback()
    with session_scope(session_factory) as session:
        session.delete(session.get(AuditLog, row_id))
        with pytest.raises(PermissionError):
            session.flush()
        session.rollback()

    # Below the ORM, the database refuses too (migration 0004 triggers).
    engine = create_db_engine(migrated_settings.database_url)
    for sql in (
        "UPDATE audit_log SET reason = 'rewritten' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(text(sql), {"id": row_id})
    with session_scope(session_factory) as session:
        assert session.get(AuditLog, row_id).reason == "x"


def test_migration_0004_adds_audit_and_correlation_columns(migrated_settings) -> None:
    insp = sa_inspect(create_db_engine(migrated_settings.database_url))
    audit_cols = {c["name"] for c in insp.get_columns("audit_log")}
    assert {"actor", "decision", "reason", "correlation_id", "job_id", "model_version"} <= audit_cols
    assert "correlation_id" in {c["name"] for c in insp.get_columns("adaptation_job")}
