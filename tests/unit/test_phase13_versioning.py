"""Phase 13 - real model registry + built-in data versioning, wired into the pipeline.

Real SQLite DB and real SQLite-backed MLflow (no mocks): content hashing, idempotent/conflicting
ingests, lineage, skops-trusted tree models, onboarding, the pipeline's post-registration
training-data snapshot + MLflow lineage tags, and the data/model HTTP APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge

from oran_adapt.core.errors import (
    ArtifactError,
    ConflictError,
    DatasetNotFoundError,
    DataVersionConflictError,
    ModelNotFoundError,
)
from oran_adapt.datastore import (
    content_hash,
    get_version,
    ingest_version,
    lineage,
    model_data_links,
)
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import ModelMetadata
from oran_adapt.registry.client import MlflowRegistry, resolve_skops_trusted_types
from oran_adapt.registry.onboarding import onboard_model

FEATURES = ["prb_util", "cqi", "rsrp"]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _frame(n: int, *, shift: float = 0.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(shift if f == "prb_util" else 0.0, 1.0, n) for f in FEATURES})
    df["target"] = 2.0 * df["prb_util"] + 0.5 * df["cqi"] + rng.normal(0, 0.1, n)
    return df


@pytest.fixture
def session_factory(migrated_settings):
    return make_session_factory(create_db_engine(migrated_settings.database_url))


@pytest.fixture
def registry(migrated_settings) -> MlflowRegistry:
    return MlflowRegistry(
        migrated_settings.mlflow_tracking_uri,
        skops_trusted_types=migrated_settings.mlflow_skops_trusted_types,
    )


# --------------------------------------------------------------------------- hashing / ingest
def test_content_hash_is_stable_and_column_order_independent():
    df = _frame(20)
    stamps = [T0 + timedelta(minutes=i) for i in range(20)]
    assert content_hash(df, stamps) == content_hash(df[list(reversed(df.columns))], stamps)
    changed = df.copy()
    changed.loc[3, "cqi"] += 1e-6
    assert content_hash(changed, stamps) != content_hash(df, stamps)
    assert content_hash(df, [s + timedelta(seconds=1) for s in stamps]) != content_hash(df, stamps)


def test_ingest_is_idempotent_and_immutable(session_factory):
    df = _frame(30)
    with session_scope(session_factory) as s:
        first = ingest_version(s, "kpi", "v1", df, kind="HISTORICAL", start=T0)
    with session_scope(session_factory) as s:
        again = ingest_version(s, "kpi", "v1", df, kind="HISTORICAL", start=T0)
    assert first.created and not again.created
    assert again.data_version_id == first.data_version_id
    assert again.content_hash == first.content_hash and again.row_count == 30

    with pytest.raises(DataVersionConflictError), session_scope(session_factory) as s:
        ingest_version(s, "kpi", "v1", df.head(10), kind="HISTORICAL", start=T0)
    with pytest.raises(ArtifactError), session_scope(session_factory) as s:
        ingest_version(s, "kpi", "empty", df.head(0), kind="HISTORICAL")


def test_timestamp_column_and_lineage_chain(session_factory):
    df = _frame(10)
    df["ts"] = [T0 + timedelta(hours=i) for i in range(10)]
    with pytest.raises(ModelNotFoundError), session_scope(session_factory) as s:
        ingest_version(s, "kpi", "x", df, kind="HISTORICAL", timestamp_column="ts",
                       model_id="ghost", model_version="1", role="TRAINING")
    with session_scope(session_factory) as s:
        s.add(ModelMetadata(model_id="m", mlflow_model_name="m", model_type="regressor",
                            framework="sklearn", task_type="regressor", target_column="target"))
        v1 = ingest_version(s, "kpi", "v1", df, kind="HISTORICAL", timestamp_column="ts")
        ingest_version(s, "kpi", "v2", _frame(10, seed=1), kind="DRIFTED", parent_version="v1",
                       start=v1.data_end + timedelta(hours=1), model_id="m", model_version="1",
                       role="DRIFT_OBSERVED")
    assert v1.data_start == T0 and v1.data_end == T0 + timedelta(hours=9)
    assert "ts" not in v1.columns
    with session_scope(session_factory) as s:
        lin = lineage(s, "kpi", "v2")
        assert [a["version"] for a in lin["ancestors"]] == ["v1"]
        assert lin["models"] == [{"model_id": "m", "model_version": "1",
                                  "role": "DRIFT_OBSERVED", "data_version": "v2"}]
        with pytest.raises(DatasetNotFoundError):
            get_version(s, "kpi", "nope")


# --------------------------------------------------------------------------- registry
def test_tree_models_register_with_trusted_types(registry):
    rf = RandomForestRegressor(n_estimators=5, max_depth=3, random_state=0)
    df = _frame(50)
    rf.fit(df[FEATURES], df["target"])
    assert resolve_skops_trusted_types(rf, registry.skops_trusted_types) == ["sklearn.tree._tree.Tree"]
    version = registry.log_model("rf_model", rf, framework="sklearn", tags={"k": "v"})
    assert version == "1"
    assert registry.describe_versions("rf_model")[0]["tags"]["k"] == "v"


def test_registry_calls_leave_no_global_mlflow_state(registry, monkeypatch):
    import os

    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("MLFLOW_REGISTRY_URI", raising=False)
    registry.log_model("leak_check", Ridge().fit([[0.0], [1.0]], [0.0, 1.0]), framework="sklearn")
    assert "MLFLOW_TRACKING_URI" not in os.environ
    assert "MLFLOW_REGISTRY_URI" not in os.environ


def test_untrusted_type_is_a_deterministic_artifact_error(migrated_settings):
    strict = MlflowRegistry(migrated_settings.mlflow_tracking_uri, skops_trusted_types=[])
    rf = RandomForestRegressor(n_estimators=2, random_state=0).fit(np.zeros((4, 1)), [0, 1, 0, 1])
    with pytest.raises(ArtifactError) as exc:
        strict.log_model("rf_strict", rf, framework="sklearn")
    assert exc.value.context["untrusted_types"] == ["sklearn.tree._tree.Tree"]


# --------------------------------------------------------------------------- onboarding
def test_onboard_links_mlflow_version_and_data(session_factory, registry):
    hist = _frame(100)
    model = Ridge().fit(hist[FEATURES], hist["target"])
    with session_scope(session_factory) as s:
        res = onboard_model(s, registry, model_id="thr", model=model, framework="sklearn",
                            task_type="regressor", target_column="target", dataset_id="thr-kpis",
                            training_frame=hist, drifted_frame=_frame(50, shift=3, seed=2))
    assert res.model_version == "1" and res.drifted_data.version == "v1-drift"
    assert res.drifted_data.data_start > res.training_data.data_end
    [v1] = registry.describe_versions("thr")
    assert v1["aliases"] == ["live"]
    assert v1["tags"]["data.training_hash"] == res.training_data.content_hash
    with session_scope(session_factory) as s:
        roles = {(link["model_version"], link["role"]) for link in model_data_links(s, "thr")}
    assert roles == {("1", "TRAINING"), ("1", "DRIFT_OBSERVED")}

    with pytest.raises(ConflictError), session_scope(session_factory) as s:
        onboard_model(s, registry, model_id="thr", model=model, framework="sklearn",
                      task_type="regressor", target_column="target", dataset_id="x",
                      training_frame=hist)


def test_onboard_rejects_missing_target_before_touching_mlflow(session_factory, registry):
    hist = _frame(20)
    with pytest.raises(ArtifactError), session_scope(session_factory) as s:
        onboard_model(s, registry, model_id="bad", model=Ridge(), framework="sklearn",
                      task_type="regressor", target_column="nope", dataset_id="d",
                      training_frame=hist)
    assert registry.client.search_registered_models() == []


# --------------------------------------------------------------------------- pipeline + API
def test_pipeline_snapshots_training_data_and_tags_lineage(client, migrated_settings):
    app = client.app
    hist = _frame(200, seed=1)
    model = Ridge().fit(hist[FEATURES], hist["target"])
    with session_scope(app.state.session_factory) as s:
        onboard_model(s, app.state.registry, model_id="thr", model=model, framework="sklearn",
                      task_type="regressor", target_column="target", dataset_id="thr-kpis",
                      training_frame=hist, drifted_frame=_frame(200, shift=3, seed=2),
                      drifted_version="drift-1")

    resp = client.post("/api/v1/adaptation/events", json={
        "model_id": "thr", "event_id": "e1", "drift_detected": True,
        "dataset_id": "thr-kpis", "drifted_data_version": "drift-1"})
    body = resp.json()
    assert body["status"] == "COMPLETED", body
    result = body["result"]
    assert result["outcome"] == "REGISTERED" and result["registered_version"] == "2"
    assert result["training_data_version"] == "train-thr-v2"

    model_view = client.get("/api/v1/models/thr").json()
    assert model_view["live_version"] == "2"
    v2 = next(v for v in model_view["versions"] if v["version"] == "2")
    assert v2["tags"]["data.training_version"] == "train-thr-v2"
    assert v2["tags"]["data.source_versions"] == "v1,drift-1"
    assert v2["tags"]["adaptation.strategy"] == "FULL_RETRAINING"

    lin = client.get("/api/v1/datasets/thr-kpis/versions/train-thr-v2/lineage").json()
    assert lin["version"]["row_count"] == 400
    assert lin["version"]["content_hash"] == v2["tags"]["data.training_hash"]
    assert [a["version"] for a in lin["ancestors"]] == ["v1"]
    assert {"model_id": "thr", "model_version": "2", "role": "TRAINING",
            "data_version": "train-thr-v2"} in lin["models"]


def test_data_api_status_codes(client):
    assert client.post("/api/v1/datasets", json={"dataset_id": "cells"}).status_code == 201
    records = _frame(12).to_dict("records")
    payload = {"version": "v1", "records": records, "start": T0.isoformat()}
    assert client.post("/api/v1/datasets/cells/versions", json=payload).status_code == 201
    assert client.post("/api/v1/datasets/cells/versions", json=payload).status_code == 200
    clash = client.post("/api/v1/datasets/cells/versions", json={**payload, "records": records[:3]})
    assert clash.status_code == 409 and clash.json()["code"] == "DATA_VERSION_CONFLICT"
    assert client.get("/api/v1/datasets").json()[0]["versions"] == ["v1"]
    assert client.get("/api/v1/datasets/cells/versions/v1").json()["row_count"] == 12
    assert client.get("/api/v1/datasets/ghost/versions").status_code == 404
    assert client.get("/api/v1/models/ghost").status_code == 404
    assert client.post("/api/v1/datasets/cells/versions",
                       json={"version": "v2", "records": []}).status_code == 422
