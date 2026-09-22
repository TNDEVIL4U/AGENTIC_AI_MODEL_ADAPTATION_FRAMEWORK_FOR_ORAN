"""Phase 10: hardening - `orchestrator.jobs.submit_adaptation_job` wraps the pure pipeline
(Phase 9) with job persistence, idempotency, concurrency-safe deduplication, retries on
transient failures, and a wall-clock timeout. Exit gate: idempotency, concurrency, retries,
timeouts - each gets its own scenario below, plus one real end-to-end run through the wrapper
and one through the actual HTTP API.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import mlflow
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

import oran_adapt.orchestrator.jobs as jobs_module
from oran_adapt.core.enums import AssociationRole, DataKind, JobStatus
from oran_adapt.core.errors import ModelNotFoundError, RegistryUnavailableError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    AdaptationEvent,
    AdaptationJob,
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
)
from oran_adapt.orchestrator.jobs import submit_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.registry.client import MlflowRegistry

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(migrated_settings):
    engine = create_db_engine(migrated_settings.database_url)
    return make_session_factory(engine)


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


def _frame(n: int, *, prb_lo: float, prb_hi: float, prb_seed: int, rsrp_seed: int) -> pd.DataFrame:
    prb = np.random.default_rng(prb_seed).uniform(prb_lo, prb_hi, size=n)
    rsrp = np.random.default_rng(rsrp_seed).uniform(-120, -60, size=n)
    label = (rsrp > -90).astype(int)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp, "label": label})


def _insert_version(session, dataset, *, version, kind, role, model_id, frame, start):
    dv = DataVersion(
        dataset_id=dataset.id,
        version=version,
        kind=kind,
        data_start=start,
        data_end=start + timedelta(days=len(frame)),
        row_count=len(frame),
    )
    session.add(dv)
    session.flush()
    for i, row in enumerate(frame.to_dict(orient="records")):
        session.add(
            DataRecord(data_version_id=dv.id, observed_at=start + timedelta(days=i), payload=row)
        )
    session.add(
        ModelDataAssociation(model_id=model_id, model_version="1", data_version_id=dv.id, role=role)
    )


def _seed_model(
    session_factory,
    registry: MlflowRegistry,
    settings,
    *,
    model_id: str,
    mlflow_name: str,
    historical: pd.DataFrame,
    drifted: pd.DataFrame,
) -> None:
    clf = LogisticRegression().fit(historical[FEATURES], historical[TARGET])
    with mlflow.start_run():
        mlflow.sklearn.log_model(clf, name="model", registered_model_name=mlflow_name)
    registry.set_alias(mlflow_name, settings.live_alias, "1")

    with session_scope(session_factory) as session:
        session.add(
            ModelMetadata(
                model_id=model_id,
                mlflow_model_name=mlflow_name,
                model_type="classification",
                framework="sklearn",
                task_type="classification",
                target_column=TARGET,
            )
        )
        dataset = DatasetMetadata(dataset_id=f"{model_id}-ds", name=model_id, schema={})
        session.add(dataset)
        session.flush()
        _insert_version(
            session, dataset, version="hist-1", kind=DataKind.HISTORICAL,
            role=AssociationRole.TRAINING, model_id=model_id, frame=historical, start=T0,
        )
        _insert_version(
            session, dataset, version="drift-1", kind=DataKind.DRIFTED,
            role=AssociationRole.DRIFT_OBSERVED, model_id=model_id, frame=drifted,
            start=T0 + timedelta(days=100),
        )


# ---- idempotency -----------------------------------------------------------------------------
def test_duplicate_event_is_deduplicated_without_rerunning(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    historical = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=2, rsrp_seed=11)
    _seed_model(
        session_factory, registry, migrated_settings,
        model_id="dedupe-model", mlflow_name="dedupe_model_mlflow",
        historical=historical, drifted=drifted,
    )
    event = DriftEvent(model_id="dedupe-model", drift_detected=False, event_id="evt-1")

    first = submit_adaptation_job(
        session_factory, event, migrated_settings, registry=registry, llm_client=None,
        workdir=str(tmp_path / "work"),
    )
    second = submit_adaptation_job(
        session_factory, event, migrated_settings, registry=registry, llm_client=None,
        workdir=str(tmp_path / "work"),
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.job_id == first.job_id
    assert second.status == JobStatus.COMPLETED

    with session_scope(session_factory) as session:
        rows = session.query(AdaptationJob).filter_by(idempotency_key=event.idempotency_key()).all()
        assert len(rows) == 1


# ---- concurrency -------------------------------------------------------------------------------
def test_concurrent_submissions_only_run_once(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    historical = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=2, rsrp_seed=11)
    _seed_model(
        session_factory, registry, migrated_settings,
        model_id="race-model", mlflow_name="race_model_mlflow",
        historical=historical, drifted=drifted,
    )
    event = DriftEvent(model_id="race-model", drift_detected=False, event_id="evt-race")
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def _submit(idx: int) -> None:
        barrier.wait(timeout=5)
        results[idx] = submit_adaptation_job(
            session_factory, event, migrated_settings, registry=registry, llm_client=None,
            workdir=str(tmp_path / "work"),
        )

    threads = [threading.Thread(target=_submit, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r is not None for r in results)
    assert {r.job_id for r in results} == {results[0].job_id}
    assert sum(1 for r in results if r.duplicate is False) == 1
    assert sum(1 for r in results if r.duplicate is True) == 1

    with session_scope(session_factory) as session:
        rows = session.query(AdaptationJob).filter_by(idempotency_key=event.idempotency_key()).all()
        assert len(rows) == 1


# ---- retries -------------------------------------------------------------------------------
def test_transient_failure_is_retried_then_succeeds(
    session_factory, migrated_settings, tmp_path, monkeypatch
) -> None:
    calls = {"n": 0}
    ok_result = JobResult(model_id="flaky-model", outcome="NO_ACTION", reason="ok on 3rd try")

    def fake_run_adaptation_job(session, event, settings, *, registry, llm_client, workdir):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RegistryUnavailableError("mlflow flaked", cause="injected")
        return ok_result

    monkeypatch.setattr(jobs_module, "run_adaptation_job", fake_run_adaptation_job)
    settings = migrated_settings.model_copy(
        update={"job_max_retries": 3, "job_retry_backoff_s": 0.01}
    )
    event = DriftEvent(model_id="flaky-model", drift_detected=False, event_id="evt-flaky")

    result = submit_adaptation_job(
        session_factory, event, settings, registry=None, llm_client=None,
        workdir=str(tmp_path / "work"),
    )

    assert calls["n"] == 3
    assert result.status == JobStatus.COMPLETED
    assert result.result["reason"] == "ok on 3rd try"


def test_retries_exhausted_marks_job_failed(
    session_factory, migrated_settings, tmp_path, monkeypatch
) -> None:
    def always_fails(session, event, settings, *, registry, llm_client, workdir):
        raise RegistryUnavailableError("mlflow is down", cause="injected")

    monkeypatch.setattr(jobs_module, "run_adaptation_job", always_fails)
    settings = migrated_settings.model_copy(
        update={"job_max_retries": 2, "job_retry_backoff_s": 0.01}
    )
    event = DriftEvent(model_id="down-model", drift_detected=False, event_id="evt-down")

    result = submit_adaptation_job(
        session_factory, event, settings, registry=None, llm_client=None,
        workdir=str(tmp_path / "work"),
    )

    assert result.status == JobStatus.FAILED
    assert result.error["code"] == "MLFLOW_UNAVAILABLE"

    with session_scope(session_factory) as session:
        events = (
            session.query(AdaptationEvent)
            .filter_by(job_id=result.job_id)
            .order_by(AdaptationEvent.id)
            .all()
        )
        retry_events = [e for e in events if "retry" in e.message]
        assert len(retry_events) == 2  # job_max_retries retries were attempted


def test_non_retryable_failure_fails_without_retry(
    session_factory, migrated_settings, tmp_path, monkeypatch
) -> None:
    calls = {"n": 0}

    def fails_deterministically(session, event, settings, *, registry, llm_client, workdir):
        calls["n"] += 1
        raise ModelNotFoundError("no such model", model="ghost-model")

    monkeypatch.setattr(jobs_module, "run_adaptation_job", fails_deterministically)
    settings = migrated_settings.model_copy(update={"job_max_retries": 3})
    event = DriftEvent(model_id="ghost-model", drift_detected=True, event_id="evt-ghost")

    result = submit_adaptation_job(
        session_factory, event, settings, registry=None, llm_client=None,
        workdir=str(tmp_path / "work"),
    )

    assert calls["n"] == 1  # never retried
    assert result.status == JobStatus.FAILED
    assert result.error["code"] == "MODEL_NOT_FOUND"


# ---- timeout -------------------------------------------------------------------------------
def test_job_exceeding_timeout_is_recorded_failed(
    session_factory, migrated_settings, tmp_path, monkeypatch
) -> None:
    import time as time_module

    def slow_run(session, event, settings, *, registry, llm_client, workdir):
        time_module.sleep(2.0)
        return JobResult(model_id="slow-model", outcome="NO_ACTION", reason="too slow")

    monkeypatch.setattr(jobs_module, "run_adaptation_job", slow_run)
    settings = migrated_settings.model_copy(
        update={"job_timeout_s": 0.2, "job_max_retries": 0}
    )
    event = DriftEvent(model_id="slow-model", drift_detected=False, event_id="evt-slow")

    start = time_module.monotonic()
    result = submit_adaptation_job(
        session_factory, event, settings, registry=None, llm_client=None,
        workdir=str(tmp_path / "work"),
    )
    elapsed = time_module.monotonic() - start

    assert elapsed < 1.5  # the call returned well before the 2s worker sleep finished
    assert result.status == JobStatus.FAILED
    assert result.error["code"] == "JOB_TIMEOUT"


# ---- end to end through the wrapper --------------------------------------------------------
def test_full_pipeline_via_job_manager_registers_and_records_events(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    historical = _frame(60, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(60, prb_lo=5.0, prb_hi=6.0, prb_seed=3, rsrp_seed=11)
    _seed_model(
        session_factory, registry, migrated_settings,
        model_id="e2e-model", mlflow_name="e2e_model_mlflow",
        historical=historical, drifted=drifted,
    )
    event = DriftEvent(model_id="e2e-model", drift_detected=True, event_id="evt-e2e")

    result = submit_adaptation_job(
        session_factory, event, migrated_settings, registry=registry, llm_client=None,
        workdir=str(tmp_path / "work"),
    )

    assert result.status == JobStatus.COMPLETED, result.error
    assert result.result["outcome"] == "REGISTERED"
    assert result.strategy == "FULL_RETRAINING"
    assert registry.get_version_by_alias("e2e_model_mlflow", migrated_settings.live_alias) == "2"

    with session_scope(session_factory) as session:
        events = (
            session.query(AdaptationEvent)
            .filter_by(job_id=result.job_id)
            .order_by(AdaptationEvent.id)
            .all()
        )
        transitions = [(e.from_status, e.to_status) for e in events]
        assert (None, "RECEIVED") in transitions
        assert ("RECEIVED", "ANALYZING") in transitions
        assert ("ANALYZING", "COMPLETED") in transitions


# ---- end to end through the HTTP API --------------------------------------------------------
def test_api_submit_event_and_idempotent_resubmit(client, migrated_settings) -> None:
    engine = create_db_engine(migrated_settings.database_url)
    api_session_factory = make_session_factory(engine)
    api_registry = MlflowRegistry(migrated_settings.mlflow_tracking_uri)
    mlflow.set_tracking_uri(migrated_settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(migrated_settings.mlflow_tracking_uri)

    historical = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=2, rsrp_seed=11)
    _seed_model(
        api_session_factory, api_registry, migrated_settings,
        model_id="api-model", mlflow_name="api_model_mlflow",
        historical=historical, drifted=drifted,
    )

    body = {"model_id": "api-model", "drift_detected": False, "event_id": "evt-api-1"}
    r1 = client.post("/api/v1/adaptation/events", json=body)
    assert r1.status_code == 201, r1.text
    j1 = r1.json()
    assert j1["status"] == "COMPLETED"
    assert j1["duplicate"] is False

    r2 = client.post("/api/v1/adaptation/events", json=body)
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    assert j2["duplicate"] is True
    assert j2["job_id"] == j1["job_id"]
