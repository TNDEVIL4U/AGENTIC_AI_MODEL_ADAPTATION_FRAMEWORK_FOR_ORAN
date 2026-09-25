"""Deterministic end-to-end demo of the adaptation pipeline (Rule 29) - no Docker, PostgreSQL or
LLM key needed. It drives the real FastAPI app (the one `uvicorn oran_adapt.api.main:app`
serves) against a real SQLite database and a real SQLite-backed MLflow registry, and walks the
fifteen stages below. Every stage checks what the system actually did and stops the demo with
exit code 1 if something is wrong; no result is hardcoded.

     1 model onboarding            6 strategy decision        11 prediction with the new LIVE
     2 historical data             7 adaptation               12 lineage
     3 drifted data                8 validation               13 audit events
     4 drift detection             9 MLflow registration      14 metrics
     5 analysis                   10 promotion                15 duplicate-event behaviour

Data is synthetic KPI data from fixed seeds (not real O-RAN data), so every run takes the same
decisions. Each run writes to a new folder under data/demo/ and deletes nothing.

Usage:
    python scripts/demo.py
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from sklearn.linear_model import Ridge

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from oran_adapt.adaptation.loaders import load_native_model
from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.db.base import session_scope
from oran_adapt.db.migrate import upgrade_to_head
from oran_adapt.db.models import AuditLog
from oran_adapt.registry.onboarding import onboard_model
from oran_adapt.registry.promotion import verify_version_artifact

FEATURES = ["prb_util", "cqi", "rsrp"]
TARGET = "throughput"
MODEL_ID = "demo-cell-throughput"
DATASET = "demo-cell-kpis"
EVENT_ID = "demo-drift-1"
ADAPTING = {"FINE_TUNING", "FULL_RETRAINING"}


class DemoFailure(RuntimeError):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def banner(n: int, title: str) -> None:
    print(f"\n[{n:2d}/15] {title}\n{'-' * 72}")


def kpi_frame(n: int, *, shift: float, seed: int) -> pd.DataFrame:
    """Synthetic cell KPIs; ``shift`` moves the PRB-utilisation distribution (the drift)."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(shift if f == "prb_util" else 0.0, 1.0, n) for f in FEATURES})
    df[TARGET] = 2.0 * df["prb_util"] + 0.5 * df["cqi"] + rng.normal(0, 0.1, n)
    return df


def metric_total(text: str, name: str) -> float:
    return sum(
        float(m.group(1))
        for m in re.finditer(rf"^{name}(?:\{{[^}}]*\}})? ([0-9.e+-]+)$", text, re.MULTILINE)
    )


def run() -> None:
    run_dir = REPO_ROOT / "data" / "demo" / datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(run_dir / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}",
        artifact_workdir=str(run_dir / "work"),
        log_json=False,
        auth_enabled=False,  # a local demo; see RUN.md section 3 for API keys
    )
    print(f"Demo folder: {run_dir}")
    upgrade_to_head(settings.database_url)
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)

    with TestClient(create_app(settings)) as client:
        app = client.app
        check(client.get("/api/v1/readiness").status_code == 200, "the API is not ready")
        before = client.get("/api/v1/metrics").text

        historical = kpi_frame(200, shift=0.0, seed=1)
        drifted = kpi_frame(200, shift=3.0, seed=2)
        model = Ridge().fit(historical[FEATURES], historical[TARGET])

        banner(1, "Model onboarding")
        with session_scope(app.state.session_factory) as s:
            onboarded = onboard_model(
                s,
                app.state.registry,
                model_id=MODEL_ID,
                model=model,
                framework="sklearn",
                task_type="regressor",
                target_column=TARGET,
                dataset_id=DATASET,
                training_frame=historical,
                drifted_frame=drifted,
                drifted_version="drift-1",
            )
        view = client.get(f"/api/v1/models/{MODEL_ID}").json()
        check(view.get("live_version") == onboarded.model_version == "1", f"onboarding: {view}")
        print(f"{MODEL_ID} registered in MLflow as version 1 and set LIVE")

        banner(2, "Historical data")
        hist = client.get(f"/api/v1/datasets/{DATASET}/versions/{onboarded.training_data.version}")
        check(hist.status_code == 200 and hist.json()["row_count"] == 200, hist.text)
        print(
            f"training data version {onboarded.training_data.version}: 200 rows, "
            f"hash {hist.json()['content_hash'][:12]}..."
        )

        banner(3, "Drifted data")
        drift = client.get(f"/api/v1/datasets/{DATASET}/versions/drift-1").json()
        check(drift.get("row_count") == 200 and drift["kind"] == "DRIFTED", f"drifted: {drift}")
        print(
            f"drifted data version drift-1: 200 rows, prb_util mean "
            f"{historical['prb_util'].mean():.2f} -> {drifted['prb_util'].mean():.2f}"
        )

        event = {
            "model_id": MODEL_ID,
            "event_id": EVENT_ID,
            "drift_detected": True,
            "dataset_id": DATASET,
            "drifted_data_version": "drift-1",
        }
        resp = client.post("/api/v1/adaptation/events", json=event)
        body = resp.json()
        check(resp.status_code == 201 and body["status"] == "COMPLETED", f"job: {body}")
        result = body["result"]
        decision = result["decision"]

        banner(4, "Drift detection")
        magnitude = decision["evidence"].get("drift_magnitude") or {}
        check(magnitude.get("n_affected", 0) >= 1, f"no drifted feature found: {magnitude}")
        print(
            f"affected features: {magnitude.get('significant_features')}, "
            f"max PSI {magnitude.get('max_psi')}, min KS p-value {magnitude.get('min_ks_pvalue')}"
        )

        banner(5, "Analysis")
        reuse = result.get("reuse_decision") or {}
        check(reuse.get("verdict"), f"no reuse analysis: {result.keys()}")
        print(
            f"reuse analysis verdict: {reuse['verdict']} "
            f"({len(result.get('version_evaluations', []))} version(s) scored)"
        )

        banner(6, "Strategy decision")
        check(decision["strategy"] in ADAPTING, f"unexpected strategy {decision['strategy']}")
        print(
            f"strategy {decision['strategy']} chosen by {decision['source']} "
            f"(confidence {decision['confidence']:.2f})"
        )
        print(f"rationale: {decision['rationale'][:200]}")

        banner(7, "Adaptation")
        candidate = result["candidate"]
        check(candidate and candidate.get("engine"), f"no candidate: {candidate}")
        print(
            f"engine {candidate['engine']}, applied strategy "
            f"{candidate.get('applied_strategy') or decision['strategy']}"
        )

        banner(8, "Validation")
        validation = result["validation"]
        check(validation["passed"], f"validation failed: {validation['reason']}")
        print(
            f"{validation['metric_name']}: current {validation['current_value']:.4f} -> "
            f"candidate {validation['candidate_value']:.4f} on "
            f"{validation['n_validation_rows']} held-out rows"
        )

        banner(9, "MLflow registration")
        new_version = result["registered_version"]
        check(result["outcome"] == "REGISTERED" and new_version == "2", f"result: {result}")
        tags = app.state.registry.get_version(MODEL_ID.replace("-", "_"), new_version).tags
        check(len(tags.get("artifact.sha256", "")) == 64, "no artifact checksum tag")
        print(f"version {new_version} registered, sha256 {tags['artifact.sha256'][:12]}...")

        banner(10, "Promotion")
        promotions = client.get(f"/api/v1/models/{MODEL_ID}/promotions").json()
        check(promotions and promotions[0]["to_version"] == new_version, f"{promotions}")
        print(
            f"LIVE moved {promotions[0]['from_version']} -> {promotions[0]['to_version']} "
            f"({promotions[0]['kind']})"
        )

        banner(11, "Prediction with the promoted model")
        local, _ = verify_version_artifact(
            app.state.registry, MODEL_ID.replace("-", "_"), new_version, str(run_dir / "predict")
        )
        live_model = load_native_model(local, "sklearn")
        sample = drifted[FEATURES].tail(5)
        predictions = live_model.predict(sample)
        check(len(predictions) == 5 and np.isfinite(predictions).all(), "bad predictions")
        error = float(np.abs(predictions - drifted[TARGET].tail(5)).mean())
        print(f"predicted 5 drifted rows, mean absolute error {error:.3f}")

        banner(12, "Lineage")
        training_version = result["training_data_version"]
        lineage = client.get(f"/api/v1/datasets/{DATASET}/versions/{training_version}/lineage")
        lin = lineage.json()
        check(lin["version"]["content_hash"] == tags["data.training_hash"], "lineage hash")
        check(
            tags.get("oran.event_id") == EVENT_ID and tags.get("oran.parent_version") == "1",
            f"lineage tags: {tags}",
        )
        print(
            f"version {new_version} <- event {EVENT_ID}, parent version 1, training data "
            f"{training_version} (from {[a['version'] for a in lin['ancestors']]})"
        )

        banner(13, "Audit events")
        with session_scope(app.state.session_factory) as s:
            actions = [
                a.action
                for a in s.query(AuditLog).filter_by(job_id=body["job_id"]).order_by(AuditLog.id)
            ]
        check(len(actions) >= 3, f"audit trail too short: {actions}")
        print(" -> ".join(actions))

        banner(14, "Metrics")
        after = client.get("/api/v1/metrics").text
        for name in (
            "drift_events_total",
            "strategy_selected_total",
            "model_registrations_total",
            "model_promotions_total",
        ):
            moved = metric_total(after, name) - metric_total(before, name)
            check(moved >= 1, f"metric {name} did not move")
            print(f"{name} +{moved:g}")

        banner(15, "Duplicate event")
        again = client.post("/api/v1/adaptation/events", json=event).json()
        check(again["duplicate"] and again["job_id"] == body["job_id"], f"duplicate: {again}")
        versions = client.get(f"/api/v1/models/{MODEL_ID}/versions").json()["versions"]
        check(len(versions) == 2, f"a duplicate event created a version: {versions}")
        print(f"same event_id returned the original job {again['job_id']}; still 2 versions")

    print(
        f"\nDemo passed: all 15 stages checked. MLflow UI:\n"
        f'  mlflow ui --backend-store-uri "{settings.mlflow_tracking_uri}"'
    )


def main() -> int:
    try:
        run()
    except DemoFailure as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
