"""Production hardening, Phase C part 3: structured API errors and a request size limit
(Rules 16/25), rejected strategies in every decision (Rule 6), the job-timeout metric and
worker metric forwarding (Rule 17)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from oran_adapt.analysis.schemas import DataVersionRef, DecisionPackage, FeatureShift
from oran_adapt.api.app import create_app
from oran_adapt.core import metrics
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.decision.engine import decide


# ---- API errors and body size -----------------------------------------------------------------
def test_unhandled_error_returns_structured_500_without_the_exception_text(migrated_settings):
    app = create_app(migrated_settings)

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError("secret-token-123 at C:/internal/path.py")

    with TestClient(app) as client:
        response = client.get("/boom", headers={"X-Correlation-ID": "trace-abc-123"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["context"] == {"correlation_id": "trace-abc-123"}
    assert response.headers["X-Correlation-ID"] == "trace-abc-123"
    assert "secret-token-123" not in response.text
    assert "Traceback" not in response.text


def test_invalid_request_is_structured_and_does_not_echo_the_input(client):
    response = client.post("/api/v1/datasets", json={"dataset_id": "", "secret": "hunter2"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["context"]["errors"]
    assert all(set(e) == {"loc", "msg", "type"} for e in body["context"]["errors"])
    assert "hunter2" not in response.text


def test_request_body_over_the_limit_is_refused_with_413(migrated_settings):
    small = migrated_settings.model_copy(update={"api_max_request_bytes": 1024})
    with TestClient(create_app(small)) as client:
        declared = client.post(
            "/api/v1/datasets", content=b"x" * 2048, headers={"Content-Type": "application/json"}
        )
        streamed = client.post(
            "/api/v1/datasets",
            content=iter([b"x" * 600, b"x" * 600]),
            headers={"Content-Type": "application/json"},
        )
        within = client.post("/api/v1/datasets", json={"dataset_id": "ok-1"})

    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["code"] == "REQUEST_TOO_LARGE"
        assert response.headers.get("X-Correlation-ID")
    assert within.status_code != 413


# ---- rejected strategies ----------------------------------------------------------------------
def _package(**overrides) -> DecisionPackage:
    shift = FeatureShift(
        feature="prb_util",
        historical_mean=1.0,
        drifted_mean=2.0,
        historical_std=0.1,
        drifted_std=0.1,
        ks_statistic=0.5,
        ks_pvalue=0.01,
        psi=0.2,
    )
    defaults = {
        "model_id": "ran-kpi-v1",
        "framework": "sklearn",
        "drift_event": DriftEvent(model_id="ran-kpi-v1", drift_detected=True),
        "reuse_reason": "significant statistical shift in: prb_util",
        "drifted_data": DataVersionRef(
            data_version_id=1, version="d1", kind="DRIFTED", row_count=100
        ),
        "feature_shifts": [shift],
        "max_psi": 0.2,
        "min_ks_pvalue": 0.01,
        "recent_performance": {"rmse": 4.5},
    }
    defaults.update(overrides)
    return DecisionPackage(**defaults)


def test_decision_lists_every_strategy_it_did_not_choose_with_a_reason():
    decision = decide(_package(max_psi=0.9, recent_performance={}), Settings(_env_file=None), None)

    assert decision.strategy == Strategy.FULL_RETRAINING
    rejected = decision.rejected_strategies
    assert set(rejected) == {Strategy.FINE_TUNING, Strategy.ROLLBACK, Strategy.NO_ACTION}
    assert "rules out fine-tuning" in rejected[Strategy.FINE_TUNING]
    assert "no recent performance" in rejected[Strategy.ROLLBACK]
    assert "preferred FULL_RETRAINING" in rejected[Strategy.NO_ACTION]


def test_forced_decision_rejects_every_adaptation_strategy_with_the_constraint():
    few_rows = DataVersionRef(data_version_id=1, version="d1", kind="DRIFTED", row_count=3)
    decision = decide(_package(drifted_data=few_rows), Settings(_env_file=None), None)

    assert decision.strategy == Strategy.INSUFFICIENT_INFORMATION
    assert len(decision.rejected_strategies) == 4
    assert all("only 3 drifted rows" in r for r in decision.rejected_strategies.values())


# ---- metrics ----------------------------------------------------------------------------------
def _value(name: str, labels: dict | None = None) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_worker_metric_deltas_are_replayed_exactly_in_the_parent():
    before = metrics.snapshot()
    metrics.RETRAIN.inc()
    metrics.SANDBOX_FAILURES.labels("scan").inc(2)
    metrics.MODEL_EVALUATION_DURATION.observe(0.3)
    delta = metrics.delta_since(before)
    retrain = _value("retrain_total")
    scan = _value("sandbox_failures_total", {"reason": "scan"})
    bucket = _value("model_evaluation_duration_seconds_bucket", {"le": "0.5"})
    below = _value("model_evaluation_duration_seconds_bucket", {"le": "0.25"})
    total = _value("model_evaluation_duration_seconds_sum")

    metrics.apply_delta(delta)  # as the parent would, after the worker reported it

    assert _value("retrain_total") == retrain + 1
    assert _value("sandbox_failures_total", {"reason": "scan"}) == scan + 2
    assert _value("model_evaluation_duration_seconds_bucket", {"le": "0.5"}) == bucket + 1
    assert _value("model_evaluation_duration_seconds_bucket", {"le": "0.25"}) == below
    assert abs(_value("model_evaluation_duration_seconds_sum") - (total + 0.3)) < 1e-9


def test_job_timeout_metric_is_exposed():
    metrics.JOB_TIMEOUTS.labels("ADAPTING").inc()
    body, _ = metrics.render()
    assert b'job_timeouts_total{stage="ADAPTING"}' in body
