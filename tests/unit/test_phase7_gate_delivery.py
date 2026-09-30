"""Hardening Phase 7: the statistical validation gate and progressive delivery.

The gate runs on real fitted models: a marginally-worse (or equal) candidate is rejected under
the default superiority policy and its decision recorded, a clear improvement is accepted, a
guardrail breach rejects whatever the primary metric says. Delivery runs the controller against
the filesystem registry with the ``registry-alias`` traffic split (read back after every
change): canary success, a canary health breach rolled back with the served version verified,
manual approval and its expiry, A/B, shadow. ``webhook`` and ``kserve`` splits run against the
serving stub and the Kubernetes emulator (unverified against real systems). The rollout metrics
adapters and the template run the conformance suite; Prometheus through httpx.MockTransport.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import deployment_emulators as demu
import httpx
import joblib
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from serving_stub import ServingStub
from sqlalchemy import inspect, select

from oran_adapt import cli, plugins
from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.adapters.deployment._common import HttpApi, StaticToken
from oran_adapt.adapters.deployment.alias import RegistryAliasDeployment
from oran_adapt.adapters.deployment.kubernetes import KServeDeployment
from oran_adapt.adapters.deployment.webhook import WebhookDeployment
from oran_adapt.adapters.rollout_metrics import (
    ApiRolloutMetrics,
    PrometheusRolloutMetrics,
    fill,
)
from oran_adapt.api.app import create_app
from oran_adapt.bootstrap import build_deployer, build_registry, build_rollout_metrics
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance import rollout_metrics as conformance
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import AuditAction, EngineKind
from oran_adapt.core.errors import (
    ConfigurationError,
    RolloutMetricsUnavailableError,
    RolloutStateError,
)
from oran_adapt.core.policies import (
    DeliveryPolicy,
    GatePolicy,
    HealthRule,
    SizeGuard,
    SliceGuard,
    load_policy_file,
    policy_hash,
)
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.migrate import downgrade_to_base, upgrade_to_head
from oran_adapt.db.models import (
    AuditLog,
    GateDecisionRecord,
    ModelMetadata,
    NotificationEvent,
    RolloutObservation,
)
from oran_adapt.delivery import controller
from oran_adapt.delivery.controller import Delivery
from oran_adapt.delivery.health import ab_test, evaluate_health
from oran_adapt.orchestrator import pipeline
from oran_adapt.orchestrator.locks import add_lock
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.ports import ArmStats, ArmWindow, DeploymentTarget
from oran_adapt.registry.deployment import Deployer
from oran_adapt.validation.engine import validate_candidate
from oran_adapt.validation.schemas import GateDecision, ValidationReport

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ["prb_util", "rsrp"]
TARGET = "label"
T0 = datetime(2030, 1, 1, tzinfo=UTC)


# ---- the gate ---------------------------------------------------------------------------------


class Flipper:
    """Predicts the true rule (label = prb_util > 0.5) but flips the rows selected by
    ``rate`` (a fraction, chosen deterministically from each row's rsrp) and, when ``cell`` is
    given, only on that cell's rows (cell = rsrp > -90)."""

    def __init__(self, rate: float, cell: int | None = None) -> None:
        self.rate = rate
        self.cell = cell

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        truth = (X["prb_util"].to_numpy() > 0.5).astype(int)
        bucket = (np.abs(X["rsrp"].to_numpy()) * 1000).astype(int) % 1000 / 1000
        flip = bucket < self.rate
        if self.cell is not None:
            flip &= (X["rsrp"].to_numpy() > -90).astype(int) == self.cell
        return np.where(flip, 1 - truth, truth)


def _frame(n: int = 400, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({"prb_util": rng.uniform(0, 1, n), "rsrp": rng.uniform(-120, -60, n)})
    return X, pd.Series((X["prb_util"] > 0.5).astype(int), name=TARGET)


def _candidate(model: object, tmp_path: Path) -> CandidateModel:
    os.makedirs(tmp_path / "candidate", exist_ok=True)
    path = str(tmp_path / "candidate" / "model.joblib")
    joblib.dump(model, path)
    return CandidateModel(engine=EngineKind.SKLEARN_FULL_RETRAIN, framework="sklearn",
                          model_class=type(model).__name__, artifact_path=path, metrics={},
                          n_train_rows=100, feature_names=FEATURES, target_column=TARGET)


def _gate(current: object, candidate: object, tmp_path: Path, policy: GatePolicy | None = None,
          context: pd.DataFrame | None = None) -> ValidationReport:
    X, y = _frame()
    settings = Settings(_env_file=None, gate_policy=policy or GatePolicy())
    return validate_candidate(_candidate(candidate, tmp_path), current, model_id="ran-kpi",
                              X=X, y=y, current_framework="sklearn",
                              estimator_type="classifier", settings=settings, context=context)


def test_marginally_worse_candidate_is_rejected_with_its_reasons(tmp_path) -> None:
    report = _gate(Flipper(0.10), Flipper(0.11), tmp_path)
    gate = report.gate
    assert gate is not None and report.passed is False
    assert gate.verdict == "REJECT" and gate.mode == "superiority"
    assert gate.delta < 0 and gate.ci_low < 0
    assert "superiority" in gate.reasons[0]
    assert gate.policy_hash == policy_hash(GatePolicy()) and gate.policy_version == "1"


def test_equal_candidate_is_rejected_by_superiority_and_accepted_by_non_inferiority(tmp_path) -> None:
    assert _gate(Flipper(0.10), Flipper(0.10), tmp_path).passed is False
    lenient = GatePolicy(mode="non_inferiority", margin=0.02)
    report = _gate(Flipper(0.10), Flipper(0.10), tmp_path, lenient)
    assert report.passed is True and report.gate.threshold == pytest.approx(-0.02)


def test_clear_improvement_is_accepted(tmp_path) -> None:
    report = _gate(Flipper(0.30), Flipper(0.02), tmp_path)
    gate = report.gate
    assert report.passed is True and gate.verdict == "ACCEPT"
    assert gate.ci_low > 0 and gate.delta > 0.2
    assert {g.name for g in gate.guardrails} >= {"latency", "size"}
    assert all(g.passed for g in gate.guardrails)


def test_the_gate_is_reproducible(tmp_path) -> None:
    first = _gate(Flipper(0.30), Flipper(0.25), tmp_path).gate
    second = _gate(Flipper(0.30), Flipper(0.25), tmp_path).gate
    assert (first.ci_low, first.ci_high) == (second.ci_low, second.ci_high)


def test_a_size_guardrail_breach_rejects_a_better_candidate(tmp_path) -> None:
    policy = GatePolicy(size=SizeGuard(max_bytes=10))
    report = _gate(Flipper(0.30), Flipper(0.02), tmp_path, policy)
    assert report.passed is False
    size = next(g for g in report.gate.guardrails if g.name == "size")
    assert size.passed is False and size.evaluated is True
    assert any("guardrail size failed" in r for r in report.gate.reasons)


def test_a_slice_regression_rejects_a_better_candidate(tmp_path) -> None:
    X, _ = _frame()
    context = X.assign(cell=np.where(X["rsrp"] > -90, "near", "far"))
    # Better overall (perfect on "near"), but "far" regresses from 30 % to 40 % errors.
    policy = GatePolicy(slices=SliceGuard(columns=["cell"], max_drop=0.05))
    report = _gate(Flipper(0.30), Flipper(0.40, cell=0), tmp_path, policy, context)
    guard = next(g for g in report.gate.guardrails if g.name == "slice:cell")
    assert guard.passed is False and "far" in guard.detail
    assert report.gate.delta > 0 and report.passed is False
    missing = _gate(Flipper(0.10), Flipper(0.02), tmp_path,
                    GatePolicy(slices=SliceGuard(columns=["band"], require_columns=True)))
    assert missing.passed is False


def test_policy_files_load_and_bad_ones_are_refused(tmp_path) -> None:
    gate = load_policy_file(str(ROOT / "config/policies/gate.toml"), GatePolicy, "gate_policy_file")
    delivery = load_policy_file(str(ROOT / "config/policies/delivery.toml"), DeliveryPolicy,
                                "delivery_policy_file")
    assert gate == GatePolicy() and delivery == DeliveryPolicy()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"canary_steps": [50, 10]}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, delivery_policy_file=str(bad))
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, gate_policy_file=str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, deployment_canary_alias="live")


# ---- online health and A/B -------------------------------------------------------------------


def _arm(values: list[float], count: int | None = None, metric: str = "error_rate") -> ArmStats:
    return ArmStats(samples={metric: values}, count=len(values) if count is None else count)


def test_health_rules() -> None:
    policy = DeliveryPolicy(min_samples=3, health=[
        HealthRule(metric="error_rate", max_degradation=0.01),
        HealthRule(metric="latency_ms", max_ratio=1.5, required=False),
        HealthRule(metric="accuracy", direction="higher", limit=0.8, required=False),
    ])
    assert evaluate_health(policy, _arm([0.0] * 2), _arm([0.0] * 2)).status == "insufficient"
    assert evaluate_health(policy, _arm([0.01] * 3), _arm([0.015] * 3)).status == "healthy"
    breach = evaluate_health(policy, _arm([0.01] * 3), _arm([0.05] * 3))
    assert breach.status == "breach" and "error_rate" in breach.reasons[0]
    slow = ArmStats(samples={"error_rate": [0.0] * 3, "latency_ms": [30.0] * 3}, count=3)
    fast = ArmStats(samples={"error_rate": [0.0] * 3, "latency_ms": [10.0] * 3}, count=3)
    assert evaluate_health(policy, fast, slow).status == "breach"
    low = ArmStats(samples={"error_rate": [0.0] * 3, "accuracy": [0.5] * 3}, count=3)
    assert "beyond its limit" in evaluate_health(policy, fast, low).reasons[0]
    missing = evaluate_health(policy, _arm([0.0] * 3), ArmStats(samples={}, count=3))
    assert missing.status == "insufficient"


def test_ab_test_verdicts() -> None:
    policy = DeliveryPolicy(min_samples=5)
    base = [0.10, 0.11, 0.09, 0.10, 0.12, 0.08, 0.10, 0.11]
    better = [v - 0.05 for v in base]
    worse = [v + 0.05 for v in base]
    assert ab_test(policy, _arm(base), _arm(better)).verdict == "better"
    assert ab_test(policy, _arm(base), _arm(worse)).verdict == "worse"
    assert ab_test(policy, _arm(base), _arm(base)).verdict == "inconclusive"
    assert ab_test(policy, _arm([0.1] * 2), _arm([0.0] * 2)).verdict == "inconclusive"
    assert ab_test(policy, _arm([0.1] * 6), _arm([0.1] * 6)).p_value == 1.0


# ---- delivery harness -------------------------------------------------------------------------


class FlakySource:
    """A rollout metrics source that can be made unreachable."""

    def __init__(self) -> None:
        self.inner = ApiRolloutMetrics()
        self.down = False

    def ping(self) -> None:
        if self.down:
            raise RolloutMetricsUnavailableError("injected: metrics source down")

    def observe(self, session, window: ArmWindow) -> ArmStats:
        self.ping()
        return self.inner.observe(session, window)


class Env:
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        self.settings = settings
        self.registry = build_registry(settings)
        self.name = f"cell_{uuid.uuid4().hex[:8]}"
        self.model_id = self.name.replace("_", "-")
        for label in ("a", "b"):
            path = tmp_path / "artifacts" / label
            path.mkdir(parents=True)
            (path / "model.onnx").write_text(f"{label}{uuid.uuid4().hex}", encoding="utf-8")
            self.registry.create_version(self.name, str(path))
        self.registry.set_alias(self.name, settings.live_alias, "1")
        self.sf = make_session_factory(create_db_engine(settings.database_url))
        with session_scope(self.sf) as session:
            session.add(ModelMetadata(model_id=self.model_id, mlflow_model_name=self.name))
        port = RegistryAliasDeployment(self.registry, settings.live_alias,
                                       canary_alias=settings.deployment_canary_alias,
                                       traffic_tag=settings.deployment_traffic_tag)
        self.deployer = Deployer(port, backend="registry-alias", timeout_s=5, poll_s=0.01,
                                 features=frozenset({"traffic_split", "instant"}))
        self.source = FlakySource()
        self.delivery = Delivery(settings=settings, registry=self.registry,
                                 deployer=self.deployer, source=self.source,
                                 workdir=str(tmp_path / "rollouts"))

    def start(self, strategy: str, policy: DeliveryPolicy, now: datetime = T0):
        with session_scope(self.sf) as session:
            row = controller.start_rollout(
                session, self.delivery, model_id=self.model_id, strategy=strategy,
                stable_version="1", candidate_version="2", policy=policy, now=now,
            )
            return row.rollout_id

    def observe(self, rid: str, at: datetime, stable: float, candidate: float,
                n: int = 20, metric: str = "error_rate") -> None:
        with session_scope(self.sf) as session:
            for arm, value in (("stable", stable), ("candidate", candidate)):
                controller.observe(session, rid, arm=arm, requests=n, metrics_={metric: value},
                                   observed_at=at)

    def tick(self, rid: str, at: datetime) -> str:
        with session_scope(self.sf) as session:
            return controller.tick(session, self.delivery, rid, now=at)

    def get(self, rid: str) -> dict:
        with session_scope(self.sf) as session:
            return controller.to_dict(controller.get_rollout(session, rid))

    def live(self) -> str:
        return self.registry.get_version_by_alias(self.name, self.settings.live_alias)

    def traffic(self):
        return self.deployer.traffic(self.name)

    def audits(self, rid: str) -> list[tuple[str, str]]:
        with session_scope(self.sf) as session:
            rows = session.scalars(select(AuditLog).where(AuditLog.model_id == self.model_id)
                                   .order_by(AuditLog.id))
            return [(r.action, r.decision) for r in rows
                    if (r.detail or {}).get("rollout_id") == rid]


@pytest.fixture
def env(migrated_settings, tmp_path) -> Env:
    settings = migrated_settings.model_copy(update={
        "registry_backend": "filesystem",
        "registry_fs_root": str(tmp_path / "registry"),
        "artifact_store_root": str(tmp_path / "store"),
    })
    return Env(settings, tmp_path)


CANARY = DeliveryPolicy(
    min_samples=10, canary_steps=[10, 50, 100], canary_step_hold_s=60,
    health=[HealthRule(metric="error_rate", max_degradation=0.01)],
)


def test_canary_success_walks_the_steps_and_promotes(env) -> None:
    rid = env.start("canary", CANARY)
    split = env.traffic()
    assert (split.stable, split.candidate, split.percent) == ("1", "2", 10)
    env.observe(rid, T0 + timedelta(seconds=20), 0.01, 0.01)
    assert env.tick(rid, T0 + timedelta(seconds=30)) == "waiting"  # hold not over yet
    assert env.tick(rid, T0 + timedelta(seconds=61)) == "CANARY"
    assert env.traffic().percent == 50 and env.get(rid)["step"] == 1
    # The new step starts its own window: nothing observed yet since then.
    assert env.tick(rid, T0 + timedelta(seconds=130)) == "waiting"
    env.observe(rid, T0 + timedelta(seconds=140), 0.02, 0.02)
    assert env.tick(rid, T0 + timedelta(seconds=200)) == "PROMOTED"
    shown = env.get(rid)
    assert shown["state"] == "PROMOTED" and shown["percent"] == 100 and not shown["active"]
    assert env.live() == "2"
    split = env.traffic()
    assert (split.stable, split.candidate, split.percent) == ("2", None, 0)
    assert [h["to"] for h in shown["history"] if "to" in h] == [
        "CANARY", "CANARY", "PROMOTED"]
    actions = [a for a, _ in env.audits(rid)]
    assert actions[0] == AuditAction.ROLLOUT_STARTED
    assert actions[-1] == AuditAction.ROLLOUT_FINISHED
    assert env.tick(rid, T0 + timedelta(seconds=300)) == "ended"
    with session_scope(env.sf) as session:
        events = [r.event_type for r in session.scalars(select(NotificationEvent))]
    assert "rollout.promoted" in events


def test_canary_breach_rolls_back_and_the_served_version_reads_back(env) -> None:
    rid = env.start("canary", CANARY)
    env.observe(rid, T0 + timedelta(seconds=20), 0.01, 0.20)
    assert env.tick(rid, T0 + timedelta(seconds=30)) == "ROLLED_BACK"
    shown = env.get(rid)
    assert shown["state"] == "ROLLED_BACK" and "health breach" in shown["reason"]
    assert shown["history"][-1]["detail"]["health"]["status"] == "breach"
    split = env.traffic()
    assert (split.stable, split.candidate, split.percent) == ("1", None, 0)
    assert env.live() == "1"
    assert env.deployer.serving(env.name) == "1"
    assert (AuditAction.ROLLOUT_FINISHED, "ROLLED_BACK") in env.audits(rid)


def test_a_model_with_a_rollout_takes_no_new_adaptation(env, tmp_path) -> None:
    rid = env.start("canary", CANARY)
    with pytest.raises(RolloutStateError):
        env.start("canary", CANARY)
    with session_scope(env.sf) as session:
        result = run_adaptation_job(session, DriftEvent(model_id=env.model_id), env.settings,
                                    registry=env.registry, llm_client=None,
                                    workdir=str(tmp_path / "job"))
    assert result.outcome == "ROLLOUT_IN_PROGRESS"
    assert result.rollout is not None and result.rollout["rollout_id"] == rid


def test_manual_approval_promotes(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=600))
    assert env.get(rid)["state"] == "AWAITING_APPROVAL"
    assert env.tick(rid, T0 + timedelta(seconds=10)) == "waiting"
    with session_scope(env.sf) as session:
        row = controller.approve(session, env.delivery, rid, actor="alice", reason="looks good",
                                 now=T0 + timedelta(seconds=60))
        assert row.state == "PROMOTED" and row.decided_by == "alice"
    assert env.live() == "2"


def test_approval_after_expiry_is_refused_and_expires(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=600))
    with session_scope(env.sf) as session, pytest.raises(RolloutStateError):
        controller.approve(session, env.delivery, rid, actor="alice",
                           now=T0 + timedelta(seconds=601))
    assert env.get(rid)["state"] == "EXPIRED" and env.live() == "1"
    with session_scope(env.sf) as session, pytest.raises(RolloutStateError):
        controller.reject(session, env.delivery, rid, actor="alice")


def test_unanswered_approval_expires_on_tick(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=600))
    assert env.tick(rid, T0 + timedelta(seconds=700)) == "EXPIRED"
    assert env.live() == "1"


def test_approval_then_canary_and_reject(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_then="canary", canary_steps=[20, 100]))
    with session_scope(env.sf) as session:
        row = controller.approve(session, env.delivery, rid, actor="alice",
                                 now=T0 + timedelta(seconds=5))
        assert row.state == "CANARY"
    assert env.traffic().percent == 20
    with session_scope(env.sf) as session:
        row = controller.reject(session, env.delivery, rid, actor="bob", reason="not now")
        assert row.state == "REJECTED" and "not now" in row.reason
    assert env.traffic().candidate is None and env.live() == "1"


def test_shadow_healthy_hands_over_to_approval(env) -> None:
    policy = DeliveryPolicy(min_samples=10, shadow_then="manual",
                            health=[HealthRule(metric="error_rate", max_degradation=0.01)])
    rid = env.start("shadow", policy)
    assert env.traffic().candidate is None  # shadow serves no traffic
    assert env.tick(rid, T0 + timedelta(seconds=5)) == "waiting"
    env.observe(rid, T0 + timedelta(seconds=10), 0.01, 0.012)
    assert env.tick(rid, T0 + timedelta(seconds=20)) == "AWAITING_APPROVAL"


def test_shadow_without_a_verdict_expires(env) -> None:
    rid = env.start("shadow", DeliveryPolicy(shadow_max_s=60))
    assert env.tick(rid, T0 + timedelta(seconds=61)) == "EXPIRED"


AB = DeliveryPolicy(min_samples=5, ab_percent=50, ab_duration_s=60,
                    health=[HealthRule(metric="error_rate", max_degradation=1.0)])


@pytest.mark.parametrize(("candidate_shift", "final", "live"),
                         [(-0.05, "PROMOTED", "2"), (0.05, "ROLLED_BACK", "1")])
def test_ab_decides_by_significance(env, candidate_shift, final, live) -> None:
    rid = env.start("ab", AB)
    assert env.traffic().percent == 50
    for i, base in enumerate([0.10, 0.11, 0.09, 0.10, 0.12, 0.08]):
        env.observe(rid, T0 + timedelta(seconds=5 + i), base, base + candidate_shift, n=2)
    assert env.tick(rid, T0 + timedelta(seconds=30)) == "waiting"  # before ab_duration_s
    assert env.tick(rid, T0 + timedelta(seconds=61)) == final
    assert env.live() == live and env.traffic().candidate is None
    assert env.get(rid)["history"][-1]["detail"]["ab"]["verdict"] in ("better", "worse")


def test_tick_is_busy_while_the_model_is_locked(env) -> None:
    rid = env.start("canary", CANARY)
    with session_scope(env.sf) as session:
        add_lock(session, env.model_id, "job:someone", 600)
    assert env.tick(rid, T0 + timedelta(seconds=90)) == "busy"
    assert env.get(rid)["state"] == "CANARY"


def test_unreachable_metrics_leave_the_rollout_unchanged(env) -> None:
    rid = env.start("canary", CANARY)
    env.source.down = True
    assert env.tick(rid, T0 + timedelta(seconds=90)) == "unavailable"
    shown = env.get(rid)
    assert shown["state"] == "CANARY" and shown["history"][-1]["note"] == "tick could not complete"
    env.source.down = False
    env.observe(rid, T0 + timedelta(seconds=95), 0.01, 0.01)
    assert env.tick(rid, T0 + timedelta(seconds=100)) == "CANARY"


def test_tick_all_and_the_active_gauge(env) -> None:
    rid = env.start("canary", CANARY)
    with session_scope(env.sf) as session:
        out = controller.tick_all(session, env.delivery, now=T0 + timedelta(seconds=5))
    assert out == {rid: "waiting"}


def test_the_worker_ticks_rollouts(env) -> None:
    from oran_adapt.orchestrator.worker import Worker

    worker = Worker(env.sf, env.settings, registry=env.registry, llm_client=None,
                    workdir=str(env.delivery.workdir))
    assert worker.tick_rollouts() == {}  # nothing active: no controller built
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=1),
                    now=datetime.now(UTC) - timedelta(seconds=10))
    assert worker.tick_rollouts() == {rid: "EXPIRED"}
    assert Worker(env.sf, env.settings, registry=None, llm_client=None,
                  workdir=".").tick_rollouts() == {}


def test_single_step_canary_promotes_at_once(env) -> None:
    rid = env.start("canary", DeliveryPolicy(canary_steps=[100]))
    assert env.get(rid)["state"] == "PROMOTED" and env.live() == "2"


def test_a_promotion_racing_a_moved_live_rolls_back(env) -> None:
    rid = env.start("manual", DeliveryPolicy())
    env.registry.set_alias(env.name, env.settings.live_alias, "2")  # someone moved LIVE
    with session_scope(env.sf) as session:
        row = controller.approve(session, env.delivery, rid, actor="alice",
                                 now=T0 + timedelta(seconds=5))
        assert row.state == "ROLLED_BACK" and "promotion failed" in row.reason


def test_observations_are_refused_for_ended_rollouts(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=1))
    env.tick(rid, T0 + timedelta(seconds=5))
    with session_scope(env.sf) as session, pytest.raises(RolloutStateError):
        controller.observe(session, rid, arm="stable", requests=1, metrics_={"x": 1.0})


# ---- the pipeline's side: gate record and delivery start --------------------------------------


def _decision(verdict: str = "ACCEPT") -> GateDecision:
    return GateDecision(
        verdict=verdict, reasons=["r"], policy_version="1", policy_hash="h" * 64,
        mode="superiority", test="paired_bootstrap", metric="accuracy", higher_is_better=True,
        current_value=0.8, candidate_value=0.9, delta=0.1, ci_low=0.05, ci_high=0.15,
        confidence=0.95, threshold=0.0, resamples=1000, n_rows=400,
    )


def test_gate_decisions_are_recorded_and_listed(env, migrated_settings) -> None:
    report = ValidationReport(model_id=env.model_id, metric_name="accuracy", current_metrics={},
                              candidate_metrics={}, current_value=0.8, candidate_value=0.79,
                              threshold=0.0, passed=False, reason="r", n_validation_rows=400,
                              gate=_decision("REJECT"))
    with session_scope(env.sf) as session:
        row = pipeline._record_gate(session, env.model_id, "1", report)
        assert row is not None and row.verdict == "REJECT"
    with session_scope(env.sf) as session:
        stored = session.scalars(select(GateDecisionRecord)).one()
        assert stored.policy_hash == "h" * 64 and stored.decision["ci_low"] == 0.05
        audit = session.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.GATE_DECIDED)).one()
        assert audit.detail["gate_decision_id"] == stored.id
    with TestClient(create_app(env.settings)) as client:
        body = client.get(f"/api/v1/models/{env.model_id}/gate-decisions").json()
    assert body[0]["verdict"] == "REJECT" and body[0]["current_version"] == "1"


def test_deliver_starts_a_canary_and_reports_delivering(env, tmp_path) -> None:
    from oran_adapt.orchestrator.schemas import JobResult

    settings = env.settings.model_copy(update={"delivery_strategy": "canary",
                                               "delivery_policy": CANARY})
    with session_scope(env.sf) as session:
        result = pipeline._deliver(
            session, settings, env.registry, env.deployer, str(tmp_path),
            model_id=env.model_id, live_version="1", new_version="2", gate_row=None,
            result=JobResult(model_id=env.model_id, outcome="DELIVERING", reason=""),
        )
    assert result.outcome == "DELIVERING" and result.rollout["state"] == "CANARY"
    assert env.traffic().percent == 10 and env.live() == "1"


# ---- traffic split adapters -------------------------------------------------------------------


@pytest.fixture(scope="module")
def stub():
    with ServingStub() as server:
        yield server


def _t(model: str, version: str) -> DeploymentTarget:
    return DeploymentTarget(model=model, version=version, source=f"model://{model}/{version}")


def test_webhook_split_reads_back_and_a_failed_split_rolls_back(stub, env) -> None:
    api = HttpApi(stub.url, service="webhook", http_factory=httpx.Client, token=StaticToken(None))
    deployer = Deployer(WebhookDeployment(api), backend="webhook", timeout_s=5, poll_s=0.01,
                        features=frozenset({"traffic_split"}))
    model = f"m{uuid.uuid4().hex[:6]}"
    split = deployer.split(model, stable=_t(model, "1"), candidate=_t(model, "2"), percent=25)
    assert (split.stable, split.candidate, split.percent) == ("1", "2", 25)
    split = deployer.split(model, stable=_t(model, "1"), candidate=None, percent=0)
    assert split.candidate is None and deployer.serving(model) == "1"
    # Through the controller: the first split fails, nothing is left on the candidate.
    env.deployer = deployer
    env.delivery.deployer = deployer
    stub.fail_next(env.name)
    rid = env.start("canary", CANARY)
    assert env.get(rid)["state"] == "ROLLED_BACK"
    assert deployer.traffic(env.name).candidate is None


def test_kserve_split_uses_canary_traffic_percent() -> None:
    kube = demu.KubeEmulator()
    api = HttpApi("https://k8s.emulator", service="the Kubernetes API",
                  http_factory=demu.MockClientFactory(kube),
                  token=StaticToken(SecretStr(demu.K8S_TOKEN)))
    port = KServeDeployment(api, namespace="serving", name_prefix="",
                            uri_template="s3://models/{name}/{version}", model_format="onnx")
    deployer = Deployer(port, backend="kserve", timeout_s=5, poll_s=0.001,
                        features=frozenset({"traffic_split"}))
    deployer.rollout(_t("kpi", "1"), None)
    split = deployer.split("kpi", stable=_t("kpi", "1"), candidate=_t("kpi", "2"), percent=10)
    assert (split.stable, split.candidate, split.percent) == ("1", "2", 10)
    split = deployer.split("kpi", stable=_t("kpi", "1"), candidate=None, percent=0)
    assert split.candidate is None and deployer.serving("kpi") == "1"


def test_startup_refuses_a_strategy_the_backend_cannot_split(tmp_path) -> None:
    base = {"registry_backend": "filesystem", "registry_fs_root": str(tmp_path / "r"),
            "artifact_store_root": str(tmp_path / "s"), "log_json": False,
            "deployment_bentoml_url": None}
    # Refused when the settings are validated, so config lint catches it too.
    with pytest.raises(ConfigurationError) as info:
        Settings(_env_file=None, delivery_strategy="canary", deployment_backend="bentoml",
                 bentoml_url="http://bento.local", **{k: v for k, v in base.items()
                                                     if k != "deployment_bentoml_url"})
    assert "registry-alias" in info.value.context["with_traffic_split"]
    assert info.value.context["key"] == "DELIVERY_STRATEGY"
    ok = Settings(_env_file=None, delivery_strategy="canary",
                  **{k: v for k, v in base.items() if k != "deployment_bentoml_url"})
    assert build_deployer(ok, build_registry(ok)).splits_traffic


# ---- rollout metrics adapters and conformance -------------------------------------------------


class PromStub:
    """A Prometheus HTTP API double: holds (rollout_id, arm, at, requests, metrics) samples and
    answers range and instant queries whose PromQL is the metric name plus the placeholders."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, float, int, dict[str, float]]] = []
        self.down = False

    def seed(self, window: ArmWindow, at: datetime, requests: int,
             metrics: dict[str, float]) -> None:
        self.rows.append((window.rollout_id, window.arm, at.timestamp(), requests, metrics))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("injected: unreachable", request=request)
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401, text="unauthorized")
        path, params = request.url.path, dict(request.url.params)
        if path == "/api/v1/status/buildinfo":
            return httpx.Response(200, json={"status": "success", "data": {}})
        name, rollout, arm = params["query"].split("|")
        if path == "/api/v1/query":
            end = float(params["time"])
            total = sum(r[3] for r in self.rows if r[0] == rollout and r[1] == arm and r[2] <= end)
            return httpx.Response(200, json={"status": "success", "data": {
                "result": [{"value": [end, str(total)]}]}})
        start, end = float(params["start"]), float(params["end"])
        values = [[r[2], str(r[4][name])] for r in self.rows
                  if r[0] == rollout and r[1] == arm and start <= r[2] <= end and name in r[4]]
        return httpx.Response(200, json={"status": "success", "data": {
            "result": [{"values": values}] if values else []}})


def _prometheus(stub_: PromStub) -> PrometheusRolloutMetrics:
    return PrometheusRolloutMetrics(
        "http://prom.local", queries={m: f"{m}|{{rollout_id}}|{{arm}}"
                                      for m in ("latency_ms", "error_rate")},
        requests_query=None, step_s=15, timeout_s=5, token=SecretStr("tok"),
        transport=httpx.MockTransport(stub_),
    )


@pytest.mark.parametrize("check", list(conformance.CHECKS))
def test_api_source_conformance(check, migrated_settings) -> None:
    sf = make_session_factory(create_db_engine(migrated_settings.database_url))
    with session_scope(sf) as session:
        def seed(window, at, requests, metrics):
            session.add(RolloutObservation(rollout_id=window.rollout_id, arm=window.arm,
                                           requests=requests, metrics=metrics, observed_at=at))
            session.flush()

        conformance.CHECKS[check](ApiRolloutMetrics(), conformance.Context(session=session,
                                                                           seed=seed))


@pytest.mark.parametrize("check", list(conformance.CHECKS))
def test_prometheus_source_conformance(check) -> None:
    prom = PromStub()
    source = _prometheus(prom)

    def down() -> None:
        prom.down = True

    conformance.CHECKS[check](source, conformance.Context(session=None, seed=prom.seed,
                                                          break_source=down))


def test_template_source_conformance(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "templates" / "rollout-metrics-adapter"))
    sys.modules.pop("adapter", None)
    import adapter  # the template module

    path = tmp_path / "obs.jsonl"
    source = adapter.JsonlRolloutMetrics(str(path))

    def seed(window, at, requests, metrics):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rollout_id": window.rollout_id, "arm": window.arm,
                                "at": at.isoformat(), "requests": requests,
                                "metrics": metrics}) + "\n")

    def down() -> None:
        source.path = str(tmp_path / "missing" / "obs.jsonl")

    assert conformance.run(source, conformance.Context(session=None, seed=seed,
                                                       break_source=down)) == list(
        conformance.CHECKS)
    sys.modules.pop("adapter", None)


def test_conformance_catches_a_source_that_mixes_arms(migrated_settings) -> None:
    class Leaky(ApiRolloutMetrics):
        def observe(self, session, window):
            other = ArmWindow(window.rollout_id, window.model, "stable", window.version,
                              window.start, window.end)
            mine, theirs = super().observe(session, window), super().observe(session, other)
            merged = {k: mine.samples.get(k, []) + theirs.samples.get(k, [])
                      for k in {*mine.samples, *theirs.samples}}
            return ArmStats(samples=merged, count=mine.count + theirs.count)

    sf = make_session_factory(create_db_engine(migrated_settings.database_url))
    with session_scope(sf) as session:
        def seed(window, at, requests, metrics):
            session.add(RolloutObservation(rollout_id=window.rollout_id, arm=window.arm,
                                           requests=requests, metrics=metrics, observed_at=at))
            session.flush()

        with pytest.raises(ConformanceFailure):
            conformance.check_arms_separate(Leaky(), conformance.Context(session=session,
                                                                         seed=seed))


def test_prometheus_fill_errors_and_settings() -> None:
    window = ArmWindow("r1", "kpi", "candidate", "2", T0, T0 + timedelta(minutes=5))
    assert fill('rate(x{model="{model}",v="{version}"}[{window_s}s])', window) == (
        'rate(x{model="kpi",v="2"}[300s])')
    prom = PromStub()
    source = _prometheus(prom)
    source.requests_query = "requests|{rollout_id}|{arm}"
    prom.seed(window, T0 + timedelta(minutes=1), 7, {"latency_ms": 5.0})
    assert source.observe(None, window).count == 7
    bad = PrometheusRolloutMetrics("http://prom.local", queries={"x": "x|r|a"},
                                   requests_query=None, step_s=15, timeout_s=5,
                                   transport=httpx.MockTransport(prom))
    with pytest.raises(RolloutMetricsUnavailableError):
        bad.ping()  # no token: 401
    with pytest.raises(ConfigurationError):
        build_rollout_metrics(Settings(_env_file=None, rollout_metrics_backend="prometheus"))
    assert isinstance(build_rollout_metrics(Settings(_env_file=None)), ApiRolloutMetrics)
    assert set(plugins.adapters("rollout_metrics")) >= {"api", "prometheus"}


# ---- migration, API, CLI ----------------------------------------------------------------------


def test_migration_0009_up_and_down(settings) -> None:
    upgrade_to_head(settings.database_url)
    engine = create_db_engine(settings.database_url)
    assert {"gate_decision", "rollout", "rollout_observation"} <= set(
        inspect(engine).get_table_names())
    engine.dispose()
    downgrade_to_base(settings.database_url)
    engine = create_db_engine(settings.database_url)
    assert "rollout" not in inspect(engine).get_table_names()
    engine.dispose()
    upgrade_to_head(settings.database_url)


def test_rollout_api(env) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=10**6), now=datetime.now(UTC))
    app = create_app(env.settings)
    with TestClient(app) as client:
        app.state.registry = env.registry
        app.state.deployer = env.deployer
        listed = client.get("/api/v1/rollouts", params={"model_id": env.model_id,
                                                         "active": True}).json()
        assert [r["rollout_id"] for r in listed["items"]] == [rid]
        assert client.get(f"/api/v1/rollouts/{rid}").json()["state"] == "AWAITING_APPROVAL"
        assert client.get("/api/v1/rollouts/nope").status_code == 404
        r = client.post(f"/api/v1/rollouts/{rid}/observations",
                        json={"arm": "candidate", "requests": 5, "metrics": {"error_rate": 0.1}})
        assert r.status_code == 201 and r.json()["requests"] == 5
        bad = client.post(f"/api/v1/rollouts/{rid}/observations",
                          json={"arm": "blue", "metrics": {"error_rate": 0.1}})
        assert bad.status_code == 422
        r = client.post(f"/api/v1/rollouts/{rid}/approve", json={"reason": "ok"})
        assert r.status_code == 200 and r.json()["state"] == "PROMOTED"
        again = client.post(f"/api/v1/rollouts/{rid}/reject", json={})
        assert again.status_code == 409
    assert env.live() == "2"


def test_rollout_cli(env, monkeypatch, capsys) -> None:
    rid = env.start("manual", DeliveryPolicy(approval_ttl_s=10**6), now=datetime.now(UTC))
    monkeypatch.setattr(cli, "get_settings", lambda: env.settings)

    def run(*argv: str):
        capsys.readouterr()
        code = cli.main(list(argv))
        return code, json.loads(capsys.readouterr().out)

    code, out = run("rollout", "list", "--active")
    assert code == 0 and [r["rollout_id"] for r in out] == [rid]
    code, out = run("rollout", "tick", "--rollout-id", rid)
    assert code == 0 and out == {rid: "waiting"}
    code, out = run("rollout", "reject", "--rollout-id", rid, "--reason", "cli")
    assert code == 0 and out["state"] == "REJECTED" and out["decided_by"].startswith("cli:")
    code, out = run("rollout", "approve", "--rollout-id", rid)
    assert code == 1 and out["code"] == "ROLLOUT_STATE_CONFLICT"
