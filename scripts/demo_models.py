"""Multi-model end-to-end demo of the full pipeline: model registry + data versioning +
adaptation.

Each demo model (sklearn, xgboost, torch; classifiers and regressors, including a
RandomForest) is onboarded with ``onboard_model``: registered in MLflow (version 1, alias
'live', tagged with its training-data hash), with its training data and an observed drifted
slice stored as content-hashed, immutable data versions linked to that model version. The real
FastAPI app then receives one drift event per model (naming the drifted data version). Each
model is built so the pipeline should take a *different* path - partial-fit fine-tuning,
sklearn/xgboost full retraining, torch fine-tuning, torch full retraining, a no-drift reuse and
a too-little-data refusal - and every outcome is checked against that expectation. Registered
candidates are reloaded through the live alias and asked for predictions.

The demo then verifies lineage (the pipeline froze the new version's training data as a data
version derived from the old baseline, linked to it, and tagged the MLflow version with it) and
runs a second drift cycle on fresh data posted through the data API, checking the analysis now
compares against the adapted model's training data rather than the stale baseline.

CPU only, small data, no Docker, no LLM key needed. Each run writes to a fresh
data/demo_models/run-<timestamp>/ directory; nothing is deleted.

Usage:
    python scripts/demo_models.py                    # per-run SQLite MLflow store
    python scripts/demo_models.py --start-server     # start a local MLflow server, use it over
                                                     # HTTP, stop it at the end
    python scripts/demo_models.py --tracking-uri http://127.0.0.1:5000   # an existing server

Exit code is 0 when every check matched its expectation, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import numpy as np
import pandas as pd
import torch
from fastapi.testclient import TestClient
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge, SGDClassifier
from torch import nn
from xgboost import XGBClassifier

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from oran_adapt.adaptation.data import holdout_size
from oran_adapt.analysis.engine import analyze
from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import session_scope
from oran_adapt.db.migrate import upgrade_to_head
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.registry.onboarding import onboard_model

torch.set_num_threads(2)
torch.manual_seed(0)

FEATURES = ["prb_util", "cqi", "rsrp"]
T0 = datetime(2026, 1, 1, tzinfo=UTC)
N_ROWS = 200
MILD_SHIFT = 0.4  # ~PSI 0.2: shifted per KS/PSI reuse thresholds, below the 0.5 fine-tune cutoff
STRONG_SHIFT = 3.0  # PSI >> 0.5: rules out fine-tuning, forces full retraining
HIST, DRIFT = "hist-1", "drift-1"


# --------------------------------------------------------------------------- demo torch models
class BeamMLP(nn.Module):
    """Two-class beam-selection classifier (logits out)."""

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Linear(3, 16), nn.ReLU(), nn.Linear(16, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class EnergyMLP(nn.Module):
    """Scalar cell-energy regressor."""

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Linear(3, 16), nn.ReLU(), nn.Linear(16, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def _train_torch(model: nn.Module, frame: pd.DataFrame, target: str, *, classifier: bool) -> nn.Module:
    X = torch.tensor(frame[FEATURES].to_numpy(), dtype=torch.float32)
    if classifier:
        y = torch.tensor(frame[target].to_numpy(), dtype=torch.long)
        loss_fn: nn.Module = nn.CrossEntropyLoss()
    else:
        y = torch.tensor(frame[target].to_numpy(), dtype=torch.float32).unsqueeze(-1)
        loss_fn = nn.MSELoss()
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    model.train()
    for _ in range(200):
        opt.zero_grad()
        loss_fn(model(X), y).backward()
        opt.step()
    model.eval()
    return model


# --------------------------------------------------------------------------- synthetic KPI data
def _features(n: int, *, shift: float, seed: int) -> pd.DataFrame:
    """Standardised KPI features; only prb_util drifts, so PSI is driven by one known feature."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "prb_util": rng.normal(shift, 1.0, n),
            "cqi": rng.normal(0.0, 1.0, n),
            "rsrp": rng.normal(0.0, 1.0, n),
        }
    )


def _class_frame(n: int, *, shift: float, seed: int) -> pd.DataFrame:
    df = _features(n, shift=shift, seed=seed)
    df["label"] = (df["cqi"] + 0.5 * df["rsrp"] - 0.3 * df["prb_util"] > 0).astype(int)
    return df


def _reg_frame(n: int, *, shift: float, seed: int) -> pd.DataFrame:
    df = _features(n, shift=shift, seed=seed)
    noise = np.random.default_rng(seed + 100).normal(0.0, 0.1, n)
    df["target"] = 2.0 * df["prb_util"] + 0.5 * df["cqi"] - 0.3 * df["rsrp"] + noise
    return df


# --------------------------------------------------------------------------- scenario catalogue
@dataclass
class Scenario:
    model_id: str
    title: str
    framework: str  # as stored in ModelMetadata and used for the MLflow flavor
    task: str  # "classification" | "regression"
    make_frame: Callable[..., pd.DataFrame]
    train: Callable[[pd.DataFrame], object]
    drift_shift: float
    drift_rows: int
    drift_detected: bool
    expect_outcome: str
    expect_strategy: str | None
    expect_engine: str | None = None

    @property
    def target(self) -> str:
        return "label" if self.task == "classification" else "target"

    @property
    def dataset_id(self) -> str:
        return f"{self.model_id}-kpis"


def _sgd(f: pd.DataFrame) -> SGDClassifier:
    return SGDClassifier(loss="log_loss", random_state=0).fit(f[FEATURES], f["label"])


SCENARIOS = [
    Scenario(
        model_id="du-anomaly-sgd",
        title="sklearn SGDClassifier, mild drift -> partial_fit fine-tuning",
        framework="sklearn", task="classification", make_frame=_class_frame, train=_sgd,
        drift_shift=MILD_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        expect_outcome="REGISTERED", expect_strategy="FINE_TUNING",
        expect_engine="SKLEARN_PARTIAL_FIT",
    ),
    Scenario(
        model_id="cell-throughput-ridge",
        title="sklearn Ridge regressor, strong drift -> full retraining (RMSE gate)",
        framework="sklearn", task="regression", make_frame=_reg_frame,
        train=lambda f: Ridge(alpha=1.0).fit(f[FEATURES], f["target"]),
        drift_shift=STRONG_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        expect_outcome="REGISTERED", expect_strategy="FULL_RETRAINING",
        expect_engine="SKLEARN_FULL_RETRAIN",
    ),
    Scenario(
        model_id="site-load-rf",
        title="sklearn RandomForest (skops-trusted tree types), strong drift -> full retraining",
        framework="sklearn", task="regression", make_frame=_reg_frame,
        train=lambda f: RandomForestRegressor(
            n_estimators=30, max_depth=6, n_jobs=1, random_state=0
        ).fit(f[FEATURES], f["target"]),
        drift_shift=STRONG_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        expect_outcome="REGISTERED", expect_strategy="FULL_RETRAINING",
        expect_engine="SKLEARN_FULL_RETRAIN",
    ),
    Scenario(
        model_id="handover-xgb",
        title="XGBClassifier, strong drift -> xgboost full retraining",
        framework="xgboost", task="classification", make_frame=_class_frame,
        train=lambda f: XGBClassifier(
            n_estimators=30, max_depth=3, n_jobs=1, random_state=0
        ).fit(f[FEATURES], f["label"]),
        drift_shift=STRONG_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        expect_outcome="REGISTERED", expect_strategy="FULL_RETRAINING",
        expect_engine="XGBOOST_FULL_RETRAIN",
    ),
    Scenario(
        model_id="beam-mlp-torch",
        title="torch MLP classifier, mild drift -> torch warm-start fine-tuning",
        framework="torch", task="classification", make_frame=_class_frame,
        train=lambda f: _train_torch(BeamMLP(), f, "label", classifier=True),
        drift_shift=MILD_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        expect_outcome="REGISTERED", expect_strategy="FINE_TUNING",
        expect_engine="TORCH_FINE_TUNE",
    ),
    Scenario(
        model_id="energy-mlp-torch",
        title="torch MLP regressor, strong drift -> torch reset-and-retrain",
        framework="torch", task="regression", make_frame=_reg_frame,
        train=lambda f: _train_torch(EnergyMLP(), f, "target", classifier=False),
        drift_shift=STRONG_SHIFT, drift_rows=N_ROWS, drift_detected=True,
        # With the retrain epoch budget from settings (torch_full_retrain_epochs=300) the
        # from-scratch model now beats the stale one on the drifted hold-out.
        expect_outcome="REGISTERED", expect_strategy="FULL_RETRAINING",
        expect_engine="TORCH_FULL_RETRAIN",
    ),
    Scenario(
        model_id="kpi-stable-sgd",
        title="control: caller reports no drift -> model reused, NO_ACTION",
        framework="sklearn", task="classification", make_frame=_class_frame, train=_sgd,
        drift_shift=0.0, drift_rows=N_ROWS, drift_detected=False,
        expect_outcome="NO_ACTION",
        expect_strategy=None,  # reuse short-circuits before any strategy is decided
    ),
    Scenario(
        model_id="sparse-site-sgd",
        title="control: only 5 drifted rows -> INSUFFICIENT_INFORMATION, nothing trained",
        framework="sklearn", task="classification", make_frame=_class_frame, train=_sgd,
        drift_shift=STRONG_SHIFT, drift_rows=5, drift_detected=True,
        expect_outcome="NO_ACTION", expect_strategy="INSUFFICIENT_INFORMATION",
    ),
]


# --------------------------------------------------------------------------- helpers
def _banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


class Demo:
    def __init__(self, settings: Settings, run_dir: Path, run_tag: str) -> None:
        self.settings = settings
        self.run_dir = run_dir
        self.run_tag = run_tag
        self.app = create_app(settings)
        self.client = TestClient(self.app)
        self.registry: MlflowRegistry = self.app.state.registry
        self.rows: list[tuple[str, str, str, str, bool]] = []

    def mlflow_name(self, model_id: str) -> str:
        # Unique per run, so repeated runs against one long-lived MLflow server never collide.
        return f"{model_id.replace('-', '_')}_{self.run_tag}"

    def live(self, model_id: str) -> str:
        return str(self.registry.get_version_by_alias(self.mlflow_name(model_id), self.settings.live_alias))

    def record(self, name: str, fw: str, strategy, outcome, ok: bool, note: str = "") -> None:
        print(f"  verdict: {'PASS' if ok else 'FAIL'}{f' ({note})' if note else ''}")
        self.rows.append((name, fw, str(strategy), str(outcome), ok))

    # ------------------------------------------------------------------ onboarding
    def onboard(self, sc: Scenario, idx: int) -> None:
        historical = sc.make_frame(N_ROWS, shift=0.0, seed=10 * idx + 1)
        drifted = sc.make_frame(sc.drift_rows, shift=sc.drift_shift, seed=10 * idx + 2)
        model = sc.train(historical)
        with session_scope(self.app.state.session_factory) as session:
            result = onboard_model(
                session,
                self.registry,
                model_id=sc.model_id,
                model=model,
                framework=sc.framework,
                task_type=sc.task,
                target_column=sc.target,
                dataset_id=sc.dataset_id,
                training_frame=historical,
                training_version=HIST,
                mlflow_model_name=self.mlflow_name(sc.model_id),
                live_alias=self.settings.live_alias,
                drifted_frame=drifted,
                drifted_version=DRIFT,
            )
        print(f"  {sc.model_id:<22} {type(model).__name__:<22} -> MLflow v{result.model_version}  "
              f"data {HIST}#{result.training_data.content_hash[:10]} ({N_ROWS} rows), "
              f"{DRIFT} ({sc.drift_rows} rows, shift {sc.drift_shift})")

    # ------------------------------------------------------------------ first drift cycle
    def reload_and_predict(self, sc: Scenario) -> str:
        from oran_adapt.adaptation.loaders import load_native_model

        name = self.mlflow_name(sc.model_id)
        version = self.live(sc.model_id)
        path = self.registry.download_artifacts(name, version, str(self.run_dir / f"reload-{sc.model_id}-v{version}"))
        model = load_native_model(path, sc.framework)
        probe = sc.make_frame(5, shift=sc.drift_shift, seed=999)[FEATURES]
        if sc.framework == "torch":
            with torch.no_grad():
                out = model(torch.tensor(probe.to_numpy(), dtype=torch.float32))
            preds = out.argmax(dim=-1).tolist() if sc.task == "classification" else out.squeeze(-1).tolist()
        else:
            preds = model.predict(probe).tolist()
        return f"v{version} predicts {[round(p, 3) if isinstance(p, float) else p for p in preds]}"

    def drift_event(self, sc: Scenario) -> None:
        print(f"\n--- {sc.model_id}: {sc.title}")
        resp = self.client.post(
            "/api/v1/adaptation/events",
            json={"model_id": sc.model_id, "event_id": f"demo-{sc.model_id}",
                  "drift_detected": sc.drift_detected, "dataset_id": sc.dataset_id,
                  "drifted_data_version": DRIFT},
        )
        body = resp.json()
        result = body.get("result") or {}
        engine = (result.get("candidate") or {}).get("engine")
        live = self.live(sc.model_id)
        print(f"  HTTP {resp.status_code}  status={body.get('status')}  strategy={body.get('strategy')}")
        print(f"  outcome={result.get('outcome')}  engine={engine}")
        if result.get("validation"):
            print(f"  validation: {result['validation'].get('reason')}")
        if result.get("outcome") == "REGISTERED":
            print(f"  reload check: {self.reload_and_predict(sc)}")
            print(f"  training data frozen as: {result.get('training_data_version')}")
        print(f"  live alias -> v{live}")

        problems = []
        if body.get("status") != "COMPLETED":
            problems.append(f"job status {body.get('status')} (error: {body.get('error')})")
        if body.get("strategy") != sc.expect_strategy:
            problems.append(f"strategy {body.get('strategy')} != {sc.expect_strategy}")
        if result.get("outcome") != sc.expect_outcome:
            problems.append(f"outcome {result.get('outcome')} != {sc.expect_outcome}")
        if sc.expect_engine and engine != sc.expect_engine:
            problems.append(f"engine {engine} != {sc.expect_engine}")
        expected_live = "2" if sc.expect_outcome == "REGISTERED" else "1"
        if live != expected_live:
            problems.append(f"live alias -> v{live}, expected v{expected_live}")
        self.record(sc.model_id, sc.framework, body.get("strategy"), result.get("outcome"),
                    not problems, "; ".join(problems) or "as expected")

    # ------------------------------------------------------------------ lineage
    def check_lineage(self, sc: Scenario) -> None:
        print(f"\n--- lineage of {sc.model_id}")
        snapshot = f"train-{sc.model_id}-v2"
        model = self.client.get(f"/api/v1/models/{sc.model_id}").json()
        v2 = next((v for v in model.get("versions", []) if v["version"] == "2"), {})
        tags = v2.get("tags", {})
        lin = self.client.get(f"/api/v1/datasets/{sc.dataset_id}/versions/{snapshot}/lineage").json()
        version = lin.get("version", {})
        ancestors = [a["version"] for a in lin.get("ancestors", [])]
        linked = {(m["model_version"], m["role"]) for m in lin.get("models", []) if m["data_version"] == snapshot}
        print(f"  MLflow v2 tags: data.training_version={tags.get('data.training_version')} "
              f"adaptation.engine={tags.get('adaptation.engine')} "
              f"data.source_versions={tags.get('data.source_versions')}")
        print(f"  data {snapshot}: rows={version.get('row_count')} "
              f"held-out={tags.get('validation.holdout_rows')} hash={str(version.get('content_hash'))[:10]} "
              f"ancestors={ancestors} linked={sorted(linked)}")
        problems = []
        if model.get("live_version") != "2":
            problems.append(f"GET /models live_version={model.get('live_version')}")
        if tags.get("data.training_version") != snapshot:
            problems.append("MLflow v2 not tagged with its training data version")
        if tags.get("data.training_hash") != version.get("content_hash"):
            problems.append("MLflow training hash != data version hash")
        if ancestors[:1] != [HIST]:
            problems.append(f"snapshot parent {ancestors[:1]} != [{HIST}]")
        if ("2", "TRAINING") not in linked:
            problems.append("snapshot not linked to v2 as TRAINING")
        # The snapshot is exactly what v2 trained on: the newest drifted rows were held out.
        held_out = holdout_size(sc.drift_rows, self.settings.validation_holdout_fraction,
                                self.settings.validation_min_rows)
        expected_rows = N_ROWS + sc.drift_rows - held_out
        if version.get("row_count") != expected_rows:
            problems.append(f"snapshot rows {version.get('row_count')} != {expected_rows}")
        if tags.get("validation.holdout_rows") != str(held_out):
            problems.append(f"validation.holdout_rows={tags.get('validation.holdout_rows')} != {held_out}")
        self.record(f"lineage:{sc.model_id}", sc.framework, "-", "-", not problems,
                    "; ".join(problems) or "MLflow <-> data lineage consistent")

    # ------------------------------------------------------------------ second cycle
    def second_cycle(self, sc: Scenario, idx: int) -> None:
        print(f"\n--- second drift cycle for {sc.model_id} (fresh data via the data API)")
        drift1 = self.client.get(f"/api/v1/datasets/{sc.dataset_id}/versions/{DRIFT}").json()
        fresh = sc.make_frame(N_ROWS, shift=sc.drift_shift, seed=10 * idx + 7)
        start = datetime.fromisoformat(drift1["data_end"]) + timedelta(minutes=1)
        payload = {"version": "drift-2", "kind": "DRIFTED", "records": fresh.to_dict("records"),
                   "start": start.isoformat(), "parent_version": DRIFT, "model_id": sc.model_id,
                   "model_version": "2", "role": "DRIFT_OBSERVED"}
        created = self.client.post(f"/api/v1/datasets/{sc.dataset_id}/versions", json=payload)
        replay = self.client.post(f"/api/v1/datasets/{sc.dataset_id}/versions", json=payload)
        conflict = self.client.post(
            f"/api/v1/datasets/{sc.dataset_id}/versions",
            json={**payload, "records": fresh.head(20).to_dict("records")},
        )
        print(f"  POST drift-2 -> {created.status_code}, identical replay -> {replay.status_code}, "
              f"different content same name -> {conflict.status_code}")

        event = DriftEvent(model_id=sc.model_id, drift_detected=True, event_id=f"demo2-{sc.model_id}",
                           dataset_id=sc.dataset_id, drifted_data_version="drift-2")
        with session_scope(self.app.state.session_factory) as session:
            baseline = analyze(session, event, self.settings)
        pkg = baseline.decision_package
        hist_ref = pkg.historical_data.version if pkg and pkg.historical_data else None
        print(f"  analysis baseline now: {hist_ref}  (was {HIST} in cycle 1)  -> {baseline.reason}")

        resp = self.client.post("/api/v1/adaptation/events", json=event.model_dump(mode="json"))
        body = resp.json()
        result = body.get("result") or {}
        print(f"  cycle-2 job: status={body.get('status')} strategy={body.get('strategy')} "
              f"outcome={result.get('outcome')} live -> v{self.live(sc.model_id)}")
        problems = []
        if (created.status_code, replay.status_code, conflict.status_code) != (201, 200, 409):
            problems.append("data API ingest/idempotency/conflict codes wrong")
        if pkg is not None and hist_ref != f"train-{sc.model_id}-v2":
            problems.append(f"baseline {hist_ref} is not the v2 training snapshot")
        if body.get("status") != "COMPLETED":
            problems.append(f"job {body.get('status')}: {body.get('error')}")
        self.record(f"cycle2:{sc.model_id}", sc.framework, body.get("strategy"),
                    result.get("outcome"), not problems, "; ".join(problems) or "as expected")


# --------------------------------------------------------------------------- main
def run(tracking_uri: str | None) -> int:
    stamp = datetime.now(UTC)
    run_dir = REPO_ROOT / "data" / "demo_models" / f"run-{stamp:%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(run_dir / 'app.db').as_posix()}",
        mlflow_tracking_uri=tracking_uri or f"sqlite:///{(run_dir / 'mlflow.db').as_posix()}",
        artifact_workdir=str(run_dir / "work"),
        log_json=False,
        log_level="WARNING",
    )

    _banner("1. Migrating the database and starting the API")
    upgrade_to_head(settings.database_url)
    demo = Demo(settings, run_dir, run_tag=f"{stamp:%H%M%S}")
    print(f"Run dir: {run_dir}\nMLflow:  {settings.mlflow_tracking_uri}")
    ready = demo.client.get("/api/v1/ready")
    print(f"GET /api/v1/ready -> {ready.status_code} {ready.json()}")
    if ready.status_code != 200:
        return 1

    _banner("2. Onboarding the demo models (MLflow v1 + versioned training/drift data)")
    for idx, sc in enumerate(SCENARIOS):
        demo.onboard(sc, idx)

    _banner("3. One drift event per model")
    for sc in SCENARIOS:
        demo.drift_event(sc)

    _banner("4. Idempotency: resubmitting one event")
    first = SCENARIOS[0]
    dup = demo.client.post(
        "/api/v1/adaptation/events",
        json={"model_id": first.model_id, "event_id": f"demo-{first.model_id}", "drift_detected": True,
              "dataset_id": first.dataset_id, "drifted_data_version": DRIFT},
    )
    print(f"POST (same event_id) -> {dup.status_code}, duplicate={dup.json().get('duplicate')}")
    demo.record("idempotent-replay", "-", "-", "-",
                dup.status_code == 200 and dup.json().get("duplicate") is True)

    _banner("5. Lineage: model versions <-> data versions")
    for sc in SCENARIOS:
        if sc.expect_outcome == "REGISTERED":
            demo.check_lineage(sc)

    _banner("6. Second drift cycle compares against the adapted model's training data")
    demo.second_cycle(SCENARIOS[1], 1)

    _banner("Summary")
    print(f"{'check':<30}{'framework':<10}{'strategy':<26}{'outcome':<12}verdict")
    for name, fw, strategy, outcome, ok in demo.rows:
        print(f"{name:<30}{fw:<10}{strategy:<26}{outcome:<12}{'PASS' if ok else 'FAIL'}")
    passed = sum(1 for r in demo.rows if r[4])
    print(f"\n{passed}/{len(demo.rows)} checks passed")
    return 0 if passed == len(demo.rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end multi-model pipeline demo")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tracking-uri", help="use an existing MLflow server / store")
    group.add_argument("--start-server", action="store_true",
                       help="start a local MLflow server for the demo and stop it afterwards")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    # MLflow prints emoji run links when talking to an HTTP server; on Windows a redirected
    # stdout defaults to cp1252 and that print would crash the run. Never fail on a log line.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.start_server:
        from run_mlflow_server import MlflowServer

        ctx = MlflowServer(port=args.port)
    else:
        ctx = nullcontext()
    with ctx as server:
        uri = server.url if server is not None else args.tracking_uri
        if server is not None:
            print(f"Started MLflow server at {uri} (it is stopped when the demo ends)")
        return run(uri)


if __name__ == "__main__":
    sys.exit(main())
