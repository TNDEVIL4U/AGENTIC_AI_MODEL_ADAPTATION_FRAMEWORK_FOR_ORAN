"""Runnable, self-contained demo of the full adaptation pipeline - no Docker, no PostgreSQL, no
LLM API key required. It seeds one model into a real (SQLite-backed) MLflow registry and a real
(SQLite) PostgreSQL-shaped database, then drives the actual FastAPI app - the same app
`uvicorn oran_adapt.api.main:app` serves - through `TestClient`, exercising two of the three
end-to-end scenarios the test suite proves: a "no drift" event that changes nothing, and a
genuine-drift event that trains, validates and registers a new model version.

The third scenario (LLM-generated fallback adaptation) needs a real ANTHROPIC_API_KEY or
GEMINI_API_KEY - see the "LLM fallback" section this script prints at the end for how to try it.

Usage:
    python scripts/demo.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from sklearn.linear_model import LogisticRegression

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import AssociationRole, DataKind
from oran_adapt.db.base import session_scope
from oran_adapt.db.migrate import upgrade_to_head
from oran_adapt.db.models import (
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
)
from oran_adapt.registry.client import MlflowRegistry

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"
MODEL_ID = "demo-cell-classifier"
MLFLOW_NAME = "demo_cell_classifier_mlflow"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _banner(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def _frame(n: int, *, prb_lo: float, prb_hi: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(prb_lo, prb_hi, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    label = (rsrp > -90).astype(int)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp, "label": label})


def _insert_version(session, dataset, *, version, kind, role, frame, start) -> None:
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
        ModelDataAssociation(
            model_id=MODEL_ID, model_version="1", data_version_id=dv.id, role=role
        )
    )


def _seed_model(session_factory, registry: MlflowRegistry, settings: Settings) -> None:
    """Trains and registers version "1" of a small classifier, plus the historical training data
    and a drifted-data snapshot it will be compared against - everything the pipeline needs to
    act on a drift event for MODEL_ID, without touching any other model."""
    historical = _frame(200, prb_lo=0.0, prb_hi=1.0, seed=1)
    drifted = _frame(200, prb_lo=5.0, prb_hi=6.0, seed=2)  # a real shift in prb_util

    clf = LogisticRegression().fit(historical[FEATURES], historical[TARGET])
    with mlflow.start_run():
        mlflow.sklearn.log_model(clf, name="model", registered_model_name=MLFLOW_NAME)
    registry.set_alias(MLFLOW_NAME, settings.live_alias, "1")

    with session_scope(session_factory) as session:
        session.add(
            ModelMetadata(
                model_id=MODEL_ID,
                mlflow_model_name=MLFLOW_NAME,
                model_type="classification",
                framework="sklearn",
                task_type="classification",
                target_column=TARGET,
            )
        )
        dataset = DatasetMetadata(dataset_id=f"{MODEL_ID}-ds", name=MODEL_ID, schema={})
        session.add(dataset)
        session.flush()
        _insert_version(
            session,
            dataset,
            version="hist-1",
            kind=DataKind.HISTORICAL,
            role=AssociationRole.TRAINING,
            frame=historical,
            start=T0,
        )
        _insert_version(
            session,
            dataset,
            version="drift-1",
            kind=DataKind.DRIFTED,
            role=AssociationRole.DRIFT_OBSERVED,
            frame=drifted,
            start=T0 + timedelta(days=100),
        )

    print(f"Seeded model '{MODEL_ID}' (MLflow: {MLFLOW_NAME}, version 1, alias '{settings.live_alias}')")


def main() -> None:
    demo_dir = REPO_ROOT / "data" / "demo"
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True)

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(demo_dir / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(demo_dir / 'mlflow.db').as_posix()}",
        artifact_workdir=str(demo_dir / "work"),
        log_json=False,
    )

    _banner("1. Migrating the database and starting the API")
    upgrade_to_head(settings.database_url)
    app = create_app(settings)
    client = TestClient(app)
    print(f"Data dir: {demo_dir}")
    print(f"Database: {settings.database_url}")
    print(f"MLflow:   {settings.mlflow_tracking_uri}")

    _banner("2. Seeding a registered model with historical + drifted data")
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    _seed_model(app.state.session_factory, app.state.registry, settings)

    _banner("3. Readiness check")
    ready = client.get("/api/v1/ready")
    print(f"GET /api/v1/ready -> {ready.status_code} {ready.json()}")

    _banner("4. Scenario A: no drift reported -> NO_ACTION")
    resp = client.post(
        "/api/v1/adaptation/events",
        json={"model_id": MODEL_ID, "event_id": "demo-no-drift", "drift_detected": False},
    )
    body = resp.json()
    print(f"POST /api/v1/adaptation/events -> {resp.status_code}")
    print(f"  job_id:   {body['job_id']}")
    print(f"  status:   {body['status']}")
    print(f"  strategy: {body['strategy']}")
    print(f"  result:   {body['result']}")

    _banner("5. Scenario B: genuine drift -> FULL_RETRAINING -> validated -> REGISTERED")
    resp = client.post(
        "/api/v1/adaptation/events",
        json={"model_id": MODEL_ID, "event_id": "demo-real-drift", "drift_detected": True},
    )
    body = resp.json()
    print(f"POST /api/v1/adaptation/events -> {resp.status_code}")
    print(f"  job_id:            {body['job_id']}")
    print(f"  status:            {body['status']}")
    print(f"  strategy:          {body['strategy']}")
    print(f"  outcome:           {body['result']['outcome'] if body['result'] else None}")
    print(f"  registered_version:{body['result']['registered_version'] if body['result'] else None}")
    print(
        "  live alias now points at version: "
        f"{app.state.registry.get_version_by_alias(MLFLOW_NAME, settings.live_alias)}"
    )

    _banner("6. Idempotency: resubmitting the same event")
    dup = client.post(
        "/api/v1/adaptation/events",
        json={"model_id": MODEL_ID, "event_id": "demo-real-drift", "drift_detected": True},
    )
    dup_body = dup.json()
    print(f"POST (same event_id) -> {dup.status_code}, duplicate={dup_body['duplicate']}, "
          f"same job_id={dup_body['job_id'] == body['job_id']}")

    _banner("Done")
    print("Inspect the run in the MLflow UI with:")
    print(f'  mlflow ui --backend-store-uri "{settings.mlflow_tracking_uri}"')
    print()
    print("LLM-generated fallback adaptation (the third scenario the test suite covers) needs a")
    print("real ANTHROPIC_API_KEY or GEMINI_API_KEY - set LLM_PROVIDER and the key in .env, then")
    print("submit an event for a model whose engine can only warm_start (not partial_fit); see")
    print("tests/unit/test_phase9_orchestrator.py's third scenario for the exact shape.")


if __name__ == "__main__":
    main()
