"""The controlled drift experiments reported in the paper: eight drift scenarios, three
remediation arms, the deployment gate against five candidates, and the per-stage cost of one
run. Every step calls the framework's own code: the Evidently PSI of analysis.comparison, the
sklearn fine-tuning (partial_fit) and full-retraining engines, and validation.engine with the
default GatePolicy (95 % paired bootstrap, 1000 resamples, superiority, margin 0).

Synthetic cell-level KPIs: prb_util, cqi and rsrp against throughput. Two generator parameters
vary independently: a shift of prb_util's mean, which changes P(X) only, and its coefficient on
throughput, which changes P(y | X). Every window is drawn with a fixed seed, so a run reproduces
the same numbers on the same library versions.

CPU only, a few seconds, nothing is written outside --out.

Usage:
    python scripts/paper_experiments.py --out results/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression, SGDRegressor

from oran_adapt.adaptation.finetune import fine_tune_sklearn
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.analysis.comparison import _evidently_drift
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import EngineKind
from oran_adapt.validation import gate as gate_mod
from oran_adapt.validation.engine import validate_candidate

FEATURES = ["prb_util", "cqi", "rsrp"]
TARGET = "throughput"
N_ROWS = 600
HOLDOUT = 0.3  # newest 30 % of each drifted window, never trained on
NOISE = 0.1
BASE_COEF = 1.0
SEED = 7
STAGES = ("data preparation", "drift analysis", "regenerate", "serialisation",
          "deployment gate")


@dataclass(frozen=True)
class Scenario:
    id: str
    shift: str
    strength: str
    prb_shift: float  # added to prb_util's mean: P(X)
    prb_coef: float  # prb_util's coefficient on throughput: P(y | X)

    @property
    def concept(self) -> bool:
        return self.prb_coef != BASE_COEF


SCENARIOS = (
    Scenario("S0", "none", "-", 0.0, BASE_COEF),
    Scenario("S1", "covariate", "mild", 0.2, BASE_COEF),
    Scenario("S2", "covariate", "strong", 0.6, BASE_COEF),
    Scenario("S3", "concept", "mild", 0.0, 1.5),
    Scenario("S4", "concept", "strong", 0.0, 2.5),
    Scenario("S5", "mixed", "mild", 0.2, 1.5),
    Scenario("S6", "mixed", "strong", 0.6, 2.5),
    Scenario("S7", "severe", "-", 0.9, 4.0),
)


def window(seed: int, *, prb_shift: float = 0.0, prb_coef: float = BASE_COEF) -> pd.DataFrame:
    """One window of N_ROWS time-ordered KPI records (features scaled to about [0, 1])."""
    rng = np.random.default_rng(seed)
    prb = rng.normal(0.5 + prb_shift, 0.1, N_ROWS)
    cqi = rng.normal(0.6, 0.15, N_ROWS)
    rsrp = rng.normal(0.4, 0.1, N_ROWS)
    y = prb_coef * prb + 0.8 * cqi + 0.5 * rsrp + rng.normal(0.0, NOISE, N_ROWS)
    return pd.DataFrame({"prb_util": prb, "cqi": cqi, "rsrp": rsrp, TARGET: y})


def split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    cut = int(len(frame) * (1 - HOLDOUT))
    return frame.iloc[:cut], frame.iloc[cut:]


def sgd() -> SGDRegressor:
    return SGDRegressor(learning_rate="constant", eta0=0.01, max_iter=2000, tol=1e-6,
                        random_state=0)


def max_psi(reference: pd.DataFrame, current: pd.DataFrame) -> float:
    drift = _evidently_drift({c: reference[c].to_numpy() for c in FEATURES},
                             {c: current[c].to_numpy() for c in FEATURES})
    return max(drift[(c, "psi")] for c in FEATURES)


def rmse(model: Any, frame: pd.DataFrame) -> float:
    pred = model.predict(frame[FEATURES])
    return float(np.sqrt(np.mean((frame[TARGET].to_numpy() - pred) ** 2)))


def _candidate(model: object, workdir: str, name: str) -> CandidateModel:
    path = os.path.join(workdir, f"{name}.joblib")
    joblib.dump(model, path)
    return CandidateModel(engine=EngineKind.SKLEARN_FULL_RETRAIN, framework="sklearn",
                          model_class=type(model).__name__, artifact_path=path,
                          n_train_rows=0, feature_names=FEATURES, target_column=TARGET)


def remediation(settings: Settings, workdir: str) -> list[dict]:
    baseline = window(SEED)
    deployed = sgd().fit(baseline[FEATURES], baseline[TARGET])
    rows = []
    for i, sc in enumerate(SCENARIOS):
        drifted = window(SEED + 100 + i, prb_shift=sc.prb_shift, prb_coef=sc.prb_coef)
        train, held = split(drifted)
        common = {"X": train[FEATURES], "y": train[TARGET], "feature_names": FEATURES,
                  "target_column": TARGET, "estimator_type": "regressor"}
        tuned = joblib.load(fine_tune_sklearn(
            deployed, artifact_dir=os.path.join(workdir, f"ft{i}"), **common).artifact_path)
        regen = joblib.load(full_retrain(
            deployed, engine=EngineKind.SKLEARN_FULL_RETRAIN, framework="sklearn",
            artifact_dir=os.path.join(workdir, f"rg{i}"), **common).artifact_path)
        none_, ft, rg = rmse(deployed, held), rmse(tuned, held), rmse(regen, held)
        rows.append({**asdict(sc), "concept": sc.concept, "max_psi": max_psi(baseline, drifted),
                     "rmse_none": none_, "rmse_finetune": ft, "rmse_regenerate": rg,
                     "gain_pct": 100 * (none_ - min(ft, rg)) / none_})
    return rows


def _bootstrap_diffs(settings: Settings, held: pd.DataFrame, incumbent: Any,
                     candidate: Any) -> np.ndarray:
    """The gate's own resampled improvements (validation.gate, same policy and seed), for the
    histograms; their quantiles are the interval the gate reports."""
    policy = settings.gate_policy
    y = held[TARGET].to_numpy()
    cur, stat = gate_mod._rows("rmse", y, incumbent.predict(held[FEATURES]), None,
                               np.arange(len(held)))
    cand, _ = gate_mod._rows("rmse", y, candidate.predict(held[FEATURES]), None,
                             np.arange(len(held)))
    return gate_mod._bootstrap(policy, cur, cand, stat, -1.0,
                               np.random.default_rng(policy.seed))


def gate(settings: Settings, workdir: str) -> list[dict]:
    """The gate against six candidates of controlled quality. The incumbent is itself fitted to
    the drifted window (as after a Regenerate), so the differences are small."""
    drifted = window(SEED + 500, prb_shift=0.2, prb_coef=1.5)
    train, held = split(drifted)
    X, y = train[FEATURES], train[TARGET]
    incumbent = sgd().fit(X, y)
    baseline = window(SEED)
    candidates = {
        "exact least squares": LinearRegression().fit(X, y),
        "SGD, other seed": SGDRegressor(learning_rate="constant", eta0=0.01, max_iter=2000,
                                        tol=1e-6, random_state=3).fit(X, y),
        "boosted stumps": GradientBoostingRegressor(n_estimators=300, max_depth=1,
                                                    random_state=0).fit(X, y),
        "copy of incumbent": incumbent,
        "stale (baseline fit)": sgd().fit(baseline[FEATURES], baseline[TARGET]),
        "constant (mean)": DummyRegressor().fit(X, y),
    }
    rows = []
    for name, model in candidates.items():
        report = validate_candidate(
            _candidate(model, workdir, "".join(c if c.isalnum() else "_" for c in name)),
            incumbent, model_id="paper", X=held[FEATURES], y=held[TARGET],
            current_framework="sklearn", estimator_type="regressor", settings=settings)
        g = report.gate
        assert g is not None  # validate_candidate always runs the gate
        diffs = _bootstrap_diffs(settings, held, incumbent, model)
        rows.append({"candidate": name, "delta": g.delta, "ci_low": g.ci_low,
                     "ci_high": g.ci_high, "verdict": g.verdict,
                     "rmse_incumbent": report.current_value,
                     "rmse_candidate": report.candidate_value,
                     "share_above_zero": float(np.mean(diffs > 0)),
                     "check_ci_low": float(np.quantile(diffs, 0.025)),
                     "diffs": diffs.tolist()})
    return rows


def cost(settings: Settings, workdir: str, repeats: int = 5) -> dict[str, dict[str, float]]:
    """Per-stage wall time of one Regenerate run (data preparation, drift analysis, Regenerate,
    serialisation, deployment gate), mean and standard deviation over ``repeats``."""
    times: dict[str, list[float]] = {}
    baseline = window(SEED)
    deployed = sgd().fit(baseline[FEATURES], baseline[TARGET])
    for r in range(repeats + 1):  # the first run warms caches and is not counted
        marks = [time.perf_counter()]
        train, held = split(window(SEED + 900, prb_shift=0.6, prb_coef=2.5))
        marks.append(time.perf_counter())
        max_psi(baseline, pd.concat([train, held]))
        marks.append(time.perf_counter())
        model = sgd().fit(train[FEATURES], train[TARGET])
        marks.append(time.perf_counter())
        cand = _candidate(model, workdir, f"cost{r}")
        marks.append(time.perf_counter())
        validate_candidate(cand, deployed, model_id="paper", X=held[FEATURES], y=held[TARGET],
                           current_framework="sklearn", estimator_type="regressor",
                           settings=settings)
        marks.append(time.perf_counter())
        if r == 0:
            continue
        for stage, start, end in zip(STAGES, marks[:-1], marks[1:], strict=True):
            times.setdefault(stage, []).append(1000 * (end - start))
    return {s: {"mean_ms": float(np.mean(v)), "std_ms": float(np.std(v, ddof=1))}
            for s, v in times.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="directory for results.json")
    args = parser.parse_args()
    settings = Settings(_env_file=None, llm_enabled=False)
    with tempfile.TemporaryDirectory() as workdir:
        results = {
            "config": {"n_rows": N_ROWS, "holdout": HOLDOUT, "noise": NOISE, "seed": SEED,
                       "gate": settings.gate_policy.model_dump(include={
                           "mode", "margin", "confidence", "resamples", "seed"})},
            "remediation": remediation(settings, workdir),
            "gate": gate(settings, workdir),
            "cost": cost(settings, workdir),
        }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
