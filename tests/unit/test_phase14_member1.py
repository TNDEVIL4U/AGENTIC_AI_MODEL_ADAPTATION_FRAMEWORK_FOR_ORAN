"""Phase 14 (Stage A): Member 1 scores every registered version on the current data before
anything is trained, reuses an existing version when one clearly beats LIVE, and every move of
LIVE goes through the promotion service (checksum-verified, recorded, undoable, rollback-able).
Jobs follow the full state machine and hold a per-model lock.

Everything runs against a real SQLite database and a real SQLite-backed MLflow in tmp_path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import mlflow
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sqlalchemy import inspect as sa_inspect

import oran_adapt.orchestrator.pipeline as pipeline_module
from oran_adapt.analysis.reuse_decision import decide_reuse
from oran_adapt.analysis.schemas import VersionEvaluation
from oran_adapt.core.enums import (
    AssociationRole,
    DataKind,
    JobStatus,
    PromotionKind,
    ReuseVerdict,
)
from oran_adapt.core.errors import (
    ArtifactIntegrityError,
    InvalidTransitionError,
    ModelBusyError,
    RegistryUnavailableError,
)
from oran_adapt.core.integrity import sha256_path, verify_checksum
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.core.state_machine import allowed_next, check_transition
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    AdaptationEvent,
    AdaptationJob,
    AuditLog,
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelLock,
    ModelMetadata,
    ModelPromotion,
    ModelVersionEvaluation,
)
from oran_adapt.orchestrator.jobs import submit_adaptation_job
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.registry.promotion import promote_version

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


# ---- data ------------------------------------------------------------------------------------
def _rsrp_regime(n: int, *, seed: int, prb_lo: float = 0.0, prb_hi: float = 1.0) -> pd.DataFrame:
    """Label depends on signal strength only."""
    rng = np.random.default_rng(seed)
    prb = rng.uniform(prb_lo, prb_hi, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp, "label": (rsrp > -90).astype(int)})


def _load_regime(n: int, *, seed: int) -> pd.DataFrame:
    """A different world: the label depends on cell load, and load is higher."""
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0.3, 1.3, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp, "label": (prb > 0.8).astype(int)})


def _fit(frame: pd.DataFrame) -> LogisticRegression:
    return LogisticRegression(max_iter=1000).fit(frame[FEATURES], frame[TARGET])


def _add_version(session, dataset, *, version, kind, role, model_id, model_version, frame, start):
    dv = DataVersion(
        dataset_id=dataset.id,
        version=version,
        kind=kind,
        data_start=start,
        data_end=start + timedelta(hours=len(frame)),
        row_count=len(frame),
    )
    session.add(dv)
    session.flush()
    for i, row in enumerate(frame.to_dict(orient="records")):
        session.add(
            DataRecord(data_version_id=dv.id, observed_at=start + timedelta(hours=i), payload=row)
        )
    session.add(
        ModelDataAssociation(
            model_id=model_id, model_version=model_version, data_version_id=dv.id, role=role
        )
    )


def _seed(
    session_factory,
    registry: MlflowRegistry,
    settings,
    *,
    model_id: str,
    name: str,
    models: list[LogisticRegression],
    live: str,
    historical: pd.DataFrame,
    drifted: pd.DataFrame,
) -> None:
    """Register ``models`` as versions 1..n, point LIVE at ``live``, and link the live version
    to its training data and to the newly observed drifted data."""
    for model in models:
        with mlflow.start_run():
            mlflow.sklearn.log_model(model, name="model", registered_model_name=name)
    registry.set_alias(name, settings.live_alias, live)
    with session_scope(session_factory) as session:
        session.add(
            ModelMetadata(
                model_id=model_id,
                mlflow_model_name=name,
                model_type="classification",
                framework="sklearn",
                task_type="classification",
                target_column=TARGET,
            )
        )
        dataset = DatasetMetadata(dataset_id=f"{model_id}-ds", name=model_id, schema={})
        session.add(dataset)
        session.flush()
        _add_version(
            session, dataset, version="hist-1", kind=DataKind.HISTORICAL,
            role=AssociationRole.TRAINING, model_id=model_id, model_version=live,
            frame=historical, start=T0,
        )
        _add_version(
            session, dataset, version="drift-1", kind=DataKind.DRIFTED,
            role=AssociationRole.DRIFT_OBSERVED, model_id=model_id, model_version=live,
            frame=drifted, start=T0 + timedelta(days=30),
        )


def _submit(session_factory, settings, registry, event, tmp_path):
    return submit_adaptation_job(
        session_factory, event, settings, registry=registry, llm_client=None,
        workdir=str(tmp_path / "work"),
    )


def _path(session_factory, job_id: str) -> list[str]:
    with session_scope(session_factory) as session:
        events = (
            session.query(AdaptationEvent)
            .filter_by(job_id=job_id)
            .order_by(AdaptationEvent.id)
            .all()
        )
        return [e.to_status for e in events]


def _seed_three_similar(session_factory, registry, settings, *, model_id, name):
    """v1, v2, v3 all trained the same way (so none beats another); LIVE = v3. The drift is a
    load shift the label does not depend on, so retraining is needed, not reuse."""
    historical = _rsrp_regime(120, seed=10)
    model = _fit(historical)
    _seed(
        session_factory, registry, settings, model_id=model_id, name=name,
        models=[model, model, model], live="3",
        historical=historical, drifted=_rsrp_regime(100, seed=11, prb_lo=5.0, prb_hi=6.0),
    )


# ---- state machine ---------------------------------------------------------------------------
def test_state_machine_allows_the_spec_path_and_refuses_jumps() -> None:
    path = [
        "RECEIVED", "VALIDATING", "DATA_PREPARING", "EVALUATING_VERSIONS", "REUSE_DECISION",
        "DECISION_PENDING", "ADAPTING", "VALIDATING_CANDIDATE", "REGISTERING", "PROMOTING",
        "COMPLETED",
    ]
    for cur, nxt in pairwise(path):
        check_transition(cur, nxt)
    check_transition("REUSE_DECISION", "PROMOTING")  # reuse skips adaptation
    check_transition("PROMOTING", "ROLLED_BACK")
    check_transition("ADAPTING", "DATA_PREPARING")  # retry after a transient failure

    with pytest.raises(InvalidTransitionError):
        check_transition("RECEIVED", "PROMOTING")
    with pytest.raises(InvalidTransitionError):
        check_transition("DATA_PREPARING", "REGISTERING")
    for terminal in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.ROLLED_BACK):
        assert allowed_next(terminal) == frozenset()
        with pytest.raises(InvalidTransitionError):
            check_transition(terminal, "DATA_PREPARING")


# ---- integrity -------------------------------------------------------------------------------
def test_checksum_of_an_artifact_directory_detects_tampering(tmp_path) -> None:
    art = tmp_path / "model"
    (art / "sub").mkdir(parents=True)
    (art / "MLmodel").write_text("flavor: sklearn\n")
    (art / "sub" / "weights.bin").write_bytes(b"\x00\x01\x02")
    digest = sha256_path(str(art))
    assert digest == sha256_path(str(art))  # stable
    verify_checksum(str(art), digest)

    (art / "sub" / "weights.bin").write_bytes(b"\x00\x01\x03")
    with pytest.raises(ArtifactIntegrityError):
        verify_checksum(str(art), digest)


# ---- reuse decision rule ---------------------------------------------------------------------
def _ev(version, value, *, live=False, compatible=True, metric="accuracy", age=1.0, n=100):
    return VersionEvaluation(
        version=version, is_live=live, compatible=compatible, metric_name=metric,
        metric_value=value if compatible else None, age_days=age, n_rows=n,
    )


def test_reuse_rule_needs_a_real_gain_and_prefers_the_newest_on_a_tie(settings) -> None:
    live = _ev("3", 0.80, live=True)
    # A gain below reuse_min_accuracy_gain (0.02) is not enough.
    small = decide_reuse([_ev("1", 0.81), live], live_version="3", max_psi=0.1, settings=settings)
    assert small.verdict == ReuseVerdict.ADAPT_MODEL
    assert small.selected_version is None

    tie = decide_reuse(
        [_ev("1", 0.90), _ev("2", 0.90), _ev("4", 0.70), live],
        live_version="3", max_psi=0.1, settings=settings,
    )
    assert tie.verdict == ReuseVerdict.REUSE_EXISTING_VERSION
    assert tie.selected_version == "2"
    assert tie.improvement == pytest.approx(0.10)
    assert tie.confidence == 1.0

    # An incompatible version is never picked, however it would have scored.
    bad = decide_reuse(
        [_ev("1", 0.99, compatible=False), live], live_version="3", max_psi=0.9, settings=settings
    )
    assert bad.verdict == ReuseVerdict.RETRAIN_MODEL  # large PSI: hint is a full retrain


def test_reuse_rule_for_regressors_and_age_limit(settings) -> None:
    live = _ev("2", 10.0, live=True, metric="rmse")
    better = decide_reuse(
        [_ev("1", 9.0, metric="rmse"), live], live_version="2", max_psi=0.1, settings=settings
    )
    assert better.verdict == ReuseVerdict.REUSE_EXISTING_VERSION  # 10% lower RMSE >= 5%
    marginal = decide_reuse(
        [_ev("1", 9.8, metric="rmse"), live], live_version="2", max_psi=0.1, settings=settings
    )
    assert marginal.verdict == ReuseVerdict.ADAPT_MODEL  # only 2% lower

    aged = settings.model_copy(update={"reuse_max_model_age_days": 30.0})
    too_old = decide_reuse(
        [_ev("1", 9.0, metric="rmse", age=90.0), live], live_version="2", max_psi=0.1,
        settings=aged,
    )
    assert too_old.verdict == ReuseVerdict.ADAPT_MODEL

    unscored = decide_reuse(
        [_ev("1", 9.0, metric="rmse"), _ev("2", 0, live=True, compatible=False)],
        live_version="2", max_psi=0.1, settings=settings,
    )
    assert unscored.verdict == ReuseVerdict.ADAPT_MODEL
    assert "LIVE could not be scored" in unscored.reason


# ---- demo scenario 1: v2 is best, goes live without training ---------------------------------
def test_best_older_version_goes_live_without_training(
    session_factory, registry, migrated_settings, client, tmp_path
) -> None:
    signal_world = _rsrp_regime(120, seed=10)
    load_world = _load_regime(120, seed=20)
    _seed(
        session_factory, registry, migrated_settings, model_id="cell-a", name="cell_a",
        models=[_fit(signal_world), _fit(load_world), _fit(_rsrp_regime(120, seed=12))],
        live="3", historical=signal_world, drifted=_load_regime(100, seed=21),
    )

    event = DriftEvent(model_id="cell-a", event_id="evt-reuse", model_version="3")
    job = _submit(session_factory, migrated_settings, registry, event, tmp_path)

    assert job.status == JobStatus.COMPLETED, job.error
    assert job.result["outcome"] == "REUSED"
    assert job.result["reused_version"] == "2"
    assert job.result["previous_live_version"] == "3"
    assert registry.get_version_by_alias("cell_a", migrated_settings.live_alias) == "2"
    assert len(registry.list_versions("cell_a")) == 3  # nothing was trained or registered

    path = _path(session_factory, job.job_id)
    assert path == [
        "RECEIVED", "VALIDATING", "DATA_PREPARING", "EVALUATING_VERSIONS", "REUSE_DECISION",
        "PROMOTING", "COMPLETED",
    ]

    evals = {e["version"]: e for e in job.result["version_evaluations"]}
    assert set(evals) == {"1", "2", "3"}
    assert all(e["compatible"] for e in evals.values())
    assert evals["2"]["metric_value"] > evals["3"]["metric_value"] + 0.02
    assert evals["3"]["is_live"] is True
    assert job.result["reuse_decision"]["verdict"] == "REUSE_EXISTING_VERSION"

    with session_scope(session_factory) as session:
        promo = session.query(ModelPromotion).filter_by(model_id="cell-a").one()
        assert (promo.kind, promo.from_version, promo.to_version, promo.status) == (
            "REUSE", "3", "2", "APPLIED",
        )
        assert promo.job_id == job.job_id
        assert len(promo.artifact_sha256) == 64
        rows = session.query(ModelVersionEvaluation).filter_by(job_id=job.job_id).all()
        assert {r.model_version: r.reusable for r in rows} == {"1": False, "2": True, "3": False}
        audit = session.query(AuditLog).filter_by(model_id="cell-a").one()
        assert audit.action == "MODEL_PROMOTED"
    tags = registry.get_version("cell_a", "2").tags
    assert tags["oran.status"] == "LIVE"
    assert registry.get_version("cell_a", "3").tags["oran.status"] == "ARCHIVED"

    # The same state through the API.
    versions = client.get("/api/v1/models/cell-a/versions").json()
    assert versions["live_version"] == "2"
    evaluation = client.get("/api/v1/models/cell-a/evaluation").json()
    assert evaluation["job_id"] == job.job_id
    assert {e["version"] for e in evaluation["evaluations"]} == {"1", "2", "3"}
    job_view = client.get(f"/api/v1/adaptation/jobs/{job.job_id}").json()
    assert [t["to_status"] for t in job_view["transitions"]] == path
    assert client.get("/api/v1/adaptation/jobs/nope").status_code == 404

    # A drift report about v3, which is no longer live, is stale: nothing happens.
    stale = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-a", event_id="evt-stale", model_version="3"), tmp_path,
    )
    assert stale.status == JobStatus.COMPLETED
    assert stale.result["outcome"] == "NO_ACTION"
    assert "stale" in stale.result["reason"]


# ---- demo scenario 2: nothing suitable, retrain to v4, then roll back ------------------------
def test_no_suitable_version_retrains_to_v4_then_rollback_restores_v3(
    session_factory, registry, migrated_settings, client, tmp_path
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-b", name="cell_b"
    )
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-b", event_id="evt-retrain"), tmp_path,
    )

    assert job.status == JobStatus.COMPLETED, job.error
    assert job.result["outcome"] == "REGISTERED"
    assert job.result["registered_version"] == "4"
    assert job.result["previous_live_version"] == "3"
    assert job.result["reuse_decision"]["verdict"] in ("ADAPT_MODEL", "RETRAIN_MODEL")
    assert registry.get_version_by_alias("cell_b", migrated_settings.live_alias) == "4"
    assert registry.get_version_by_alias("cell_b", migrated_settings.candidate_alias) == "4"
    v4 = registry.get_version("cell_b", "4").tags
    assert len(v4["artifact.sha256"]) == 64
    assert v4["oran.status"] == "LIVE"
    assert "ADAPTING" in _path(session_factory, job.job_id)

    # Roll back through the API: LIVE returns to v3, the version LIVE held before v4.
    r = client.post(
        "/api/v1/models/cell-b/rollback", json={"reason": "operator", "idempotency_key": "rb-1"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["kind"], body["from_version"], body["to_version"], body["status"]) == (
        "ROLLBACK", "4", "3", "APPLIED",
    )
    assert registry.get_version_by_alias("cell_b", migrated_settings.live_alias) == "3"

    # Resending the same rollback request changes nothing.
    again = client.post(
        "/api/v1/models/cell-b/rollback", json={"reason": "operator", "idempotency_key": "rb-1"}
    )
    assert again.json()["promotion_id"] == body["promotion_id"]
    assert registry.get_version_by_alias("cell_b", migrated_settings.live_alias) == "3"

    history = client.get("/api/v1/models/cell-b/promotions").json()
    assert [(h["kind"], h["from_version"], h["to_version"]) for h in history] == [
        ("ROLLBACK", "4", "3"),
        ("PROMOTE_CANDIDATE", "3", "4"),
    ]
    assert client.post(
        "/api/v1/models/cell-b/rollback", json={"target_version": "99"}
    ).status_code == 404
    with session_scope(session_factory) as session:
        assert session.get(ModelLock, "cell-b") is None  # every lock was released


# ---- demo scenario 3: validation fails, LIVE unchanged ---------------------------------------
def test_failed_validation_leaves_live_untouched(
    session_factory, registry, migrated_settings, tmp_path, monkeypatch
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-c", name="cell_c"
    )
    real_validate = pipeline_module.validate_candidate

    def failing_validate(*args, **kwargs):
        # The real gate runs; only its verdict is forced to FAIL, as a worse candidate would.
        report = real_validate(*args, **kwargs)
        return report.model_copy(update={"passed": False, "reason": "forced FAIL for test"})

    monkeypatch.setattr(pipeline_module, "validate_candidate", failing_validate)
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-c", event_id="evt-reject"), tmp_path,
    )

    assert job.status == JobStatus.COMPLETED, job.error
    assert job.result["outcome"] == "REJECTED"
    assert registry.get_version_by_alias("cell_c", migrated_settings.live_alias) == "3"
    assert len(registry.list_versions("cell_c")) == 3
    assert _path(session_factory, job.job_id)[-2:] == ["VALIDATING_CANDIDATE", "COMPLETED"]
    with session_scope(session_factory) as session:
        assert session.query(ModelPromotion).filter_by(model_id="cell-c").count() == 0


# ---- promotion failure: alias restored, job ROLLED_BACK --------------------------------------
def test_failed_promotion_restores_live_and_marks_job_rolled_back(
    session_factory, registry, migrated_settings, tmp_path, monkeypatch
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-d", name="cell_d"
    )
    real_tags = registry.set_version_tags

    def flaky_tags(name, version, tags):
        if tags.get("oran.status") == "LIVE":
            raise RegistryUnavailableError("MLflow went away mid-promotion")
        return real_tags(name, version, tags)

    monkeypatch.setattr(registry, "set_version_tags", flaky_tags)
    job = _submit(
        session_factory, migrated_settings, registry,
        DriftEvent(model_id="cell-d", event_id="evt-promo-fail"), tmp_path,
    )

    assert job.status == JobStatus.ROLLED_BACK, job.error
    assert job.result["outcome"] == "ROLLED_BACK"
    assert job.result["registered_version"] == "4"
    assert registry.get_version_by_alias("cell_d", migrated_settings.live_alias) == "3"
    assert _path(session_factory, job.job_id)[-2:] == ["PROMOTING", "ROLLED_BACK"]
    with session_scope(session_factory) as session:
        assert session.query(ModelPromotion).filter_by(model_id="cell-d").count() == 0


# ---- integrity at promotion time -------------------------------------------------------------
def test_promotion_refuses_a_version_whose_artifact_changed(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-e", name="cell_e"
    )
    registry.set_version_tags("cell_e", "2", {"artifact.sha256": "0" * 64})
    with session_scope(session_factory) as session, pytest.raises(ArtifactIntegrityError):
        promote_version(
            session, registry, model_id="cell-e", version="2", kind=PromotionKind.ROLLBACK,
            live_alias=migrated_settings.live_alias, workdir=str(tmp_path / "w"),
        )
    assert registry.get_version_by_alias("cell_e", migrated_settings.live_alias) == "3"

    # Promoting what is already live is a recorded no-op.
    with session_scope(session_factory) as session:
        same = promote_version(
            session, registry, model_id="cell-e", version="3", kind=PromotionKind.ROLLBACK,
            live_alias=migrated_settings.live_alias, workdir=str(tmp_path / "w"),
        )
    assert same.status == "NO_CHANGE"


# ---- per-model lock --------------------------------------------------------------------------
def test_second_event_for_a_busy_model_is_refused_until_the_lock_expires(
    session_factory, registry, migrated_settings, client, tmp_path
) -> None:
    _seed_three_similar(
        session_factory, registry, migrated_settings, model_id="cell-f", name="cell_f"
    )
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        session.add(
            ModelLock(
                model_id="cell-f", job_id="other-job", acquired_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )

    event = DriftEvent(model_id="cell-f", event_id="evt-busy", drift_detected=False)
    with pytest.raises(ModelBusyError) as busy:
        _submit(session_factory, migrated_settings, registry, event, tmp_path)
    assert busy.value.context["running_job_id"] == "other-job"
    r = client.post("/api/v1/adaptation/events", json=event.model_dump(mode="json"))
    assert r.status_code == 409
    assert r.json()["code"] == "MODEL_BUSY"
    assert client.post("/api/v1/models/cell-f/rollback", json={}).status_code == 409
    with session_scope(session_factory) as session:
        assert session.query(AdaptationJob).filter_by(model_id="cell-f").count() == 0

    # The holder died: once its lock expires, the next event takes it over and runs.
    with session_scope(session_factory) as session:
        session.get(ModelLock, "cell-f").expires_at = now - timedelta(seconds=1)
    job = _submit(session_factory, migrated_settings, registry, event, tmp_path)
    assert job.status == JobStatus.COMPLETED, job.error
    with session_scope(session_factory) as session:
        assert session.get(ModelLock, "cell-f") is None


# ---- contracts -------------------------------------------------------------------------------
def test_team1_event_fields_keep_legacy_idempotency_keys() -> None:
    legacy = DriftEvent(model_id="m", drift_detected=True, drift_score=0.4)
    body = legacy.model_dump(mode="json")
    for name in (
        "model_version", "timestamp", "drift_type", "severity", "drift_metrics",
        "affected_features", "source_dataset_version", "data_start_time", "data_end_time",
    ):
        body.pop(name)
    import hashlib
    import json

    expected = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
    assert legacy.idempotency_key() == f"m:{expected}"

    richer = DriftEvent(
        model_id="m", drift_score=0.4, severity="HIGH", drift_type="feature",
        affected_features=["prb_util"], drift_metrics={"psi": 0.7},
    )
    assert richer.drift_detected is True
    assert richer.idempotency_key() != legacy.idempotency_key()
    with pytest.raises(ValueError):
        DriftEvent(model_id="m", data_start_time=T0, data_end_time=T0 - timedelta(days=1))


def test_migration_creates_the_phase14_tables(migrated_settings) -> None:
    engine = create_db_engine(migrated_settings.database_url)
    names = set(sa_inspect(engine).get_table_names())
    assert {"model_promotion", "model_lock", "model_version_evaluation"} <= names
    indexes = {i["name"] for i in sa_inspect(engine).get_indexes("model_promotion")}
    assert "ix_model_promotion_model_id" in indexes
    engine.dispose()
