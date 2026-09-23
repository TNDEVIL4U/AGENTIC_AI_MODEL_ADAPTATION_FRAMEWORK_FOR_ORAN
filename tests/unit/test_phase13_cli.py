"""Phase 13 - the ``oran-adapt`` operator CLI, end to end.

Drives ``oran_adapt.cli.main`` exactly as the console script does (argv in, JSON on stdout, exit
code out) against a real SQLite DB and a real SQLite-backed MLflow (no mocks): migrate, ingest,
list, lineage, onboard a joblib model, show it, submit a drift event through the full pipeline,
and the structured-error / exit-code-1 path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import Ridge

from oran_adapt import cli

FEATURES = ["prb_util", "cqi", "rsrp"]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _frame(n: int, *, shift: float = 0.0, seed: int = 0, start: datetime = T0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(shift if f == "prb_util" else 0.0, 1.0, n) for f in FEATURES})
    df["target"] = 2.0 * df["prb_util"] + 0.5 * df["cqi"] + rng.normal(0, 0.1, n)
    df["ts"] = [(start + timedelta(minutes=i)).isoformat() for i in range(n)]
    return df


@pytest.fixture
def run(settings, monkeypatch, capsys):
    """Invoke the CLI with ``settings`` in place of the .env-driven ones; return (code, json)."""
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def _run(*argv: str):
        capsys.readouterr()  # drop anything printed by earlier calls
        code = cli.main(list(argv))
        return code, json.loads(capsys.readouterr().out)

    return _run


def test_cli_end_to_end(run, tmp_path):
    hist = _frame(200, seed=1)
    drift = _frame(200, shift=3, seed=2, start=T0 + timedelta(days=1))
    hist_csv, drift_csv = tmp_path / "hist.csv", tmp_path / "drift.csv"
    hist.to_csv(hist_csv, index=False)
    drift.to_csv(drift_csv, index=False)
    model_file = tmp_path / "thr.joblib"
    joblib.dump(Ridge().fit(hist[FEATURES], hist["target"]), model_file)

    assert run("db", "upgrade") == (0, {"database": "upgraded to head"})

    code, onboarded = run(
        "model", "onboard", "--model-id", "thr", "--model-file", str(model_file),
        "--framework", "sklearn", "--task-type", "regressor", "--target", "target",
        "--dataset", "thr-kpis", "--training-csv", str(hist_csv), "--timestamp-column", "ts")
    assert code == 0, onboarded
    assert onboarded["model_version"] == "1"

    code, drifted = run(
        "data", "ingest", "--dataset", "thr-kpis", "--version", "drift-1", "--csv", str(drift_csv),
        "--kind", "DRIFTED", "--timestamp-column", "ts", "--parent", "v1",
        "--model-id", "thr", "--model-version", "1", "--role", "DRIFT_OBSERVED")
    assert code == 0, drifted
    assert drifted["created"] and drifted["row_count"] == 200 and "ts" not in drifted["columns"]
    # Re-ingesting identical content is idempotent, not an error.
    code, again = run(
        "data", "ingest", "--dataset", "thr-kpis", "--version", "drift-1", "--csv", str(drift_csv),
        "--kind", "DRIFTED", "--timestamp-column", "ts", "--parent", "v1")
    assert code == 0 and not again["created"]
    assert again["content_hash"] == drifted["content_hash"]

    code, versions = run("data", "list", "--dataset", "thr-kpis")
    assert code == 0 and sorted(v["version"] for v in versions) == ["drift-1", "v1"]

    code, lin = run("data", "lineage", "--dataset", "thr-kpis", "--version", "drift-1")
    assert code == 0
    assert [a["version"] for a in lin["ancestors"]] == ["v1"]
    assert {"model_id": "thr", "model_version": "1", "role": "DRIFT_OBSERVED",
            "data_version": "drift-1"} in lin["models"]

    code, job = run("event", "submit", "--model-id", "thr", "--dataset", "thr-kpis",
                    "--drifted-version", "drift-1", "--event-id", "e1")
    assert code == 0 and job["status"] == "COMPLETED", job
    assert job["result"]["outcome"] == "REGISTERED"
    assert job["result"]["registered_version"] == "2"
    assert job["result"]["training_data_version"] == "train-thr-v2"

    code, shown = run("model", "show", "--model-id", "thr")
    assert code == 0 and shown["mlflow_model_name"] == "thr"
    live = [v["version"] for v in shown["versions"] if "live" in v["aliases"]]
    assert live == ["2"]
    assert {(link["model_version"], link["role"]) for link in shown["data_links"]} >= {
        ("1", "TRAINING"), ("1", "DRIFT_OBSERVED"), ("2", "TRAINING")}


def test_cli_errors_are_structured_json_with_exit_code_1(run, tmp_path):
    assert run("db", "upgrade")[0] == 0

    code, err = run("model", "show", "--model-id", "ghost")
    assert code == 1 and err["code"] == "MODEL_NOT_FOUND"

    csv = tmp_path / "a.csv"
    ingest = ("data", "ingest", "--dataset", "d", "--version", "v1", "--csv", str(csv),
              "--start", T0.isoformat())
    _frame(10).drop(columns="ts").to_csv(csv, index=False)
    assert run(*ingest)[1]["created"]
    # Without a timestamp column, --start pins the row times, so the same CSV is idempotent.
    code, again = run(*ingest)
    assert code == 0 and not again["created"]
    _frame(5).drop(columns="ts").to_csv(csv, index=False)
    code, err = run(*ingest)
    assert code == 1 and err["code"] == "DATA_VERSION_CONFLICT"
