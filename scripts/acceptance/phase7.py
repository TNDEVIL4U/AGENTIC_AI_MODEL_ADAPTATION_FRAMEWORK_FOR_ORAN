"""Hardening Phase 7 acceptance: the validation gate and progressive delivery, end to end.

Run by scripts/verify.sh 7 after lint, the import-boundary test and the scoped tests.

1. A marginally-worse candidate is rejected under the default (superiority) gate policy, and
   the decision is recorded: gate_decision row, GATE_DECIDED audit entry, and
   GET /api/v1/models/{id}/gate-decisions, with the policy hash.
2. A clear improvement is accepted.
3. A guardrail breach (a slice regression) rejects a candidate that is better overall.
4. A canary walks its steps on healthy metrics and promotes: LIVE is the candidate, the split
   is gone.
5. A canary health breach rolls back automatically; the serving system is read back and
   reports all traffic on the stable version, LIVE never moved.
6. Manual approval promotes; an approval after its deadline is refused and the rollout is
   EXPIRED.
7. Vendor SDKs stay inside their adapters; every rollout metrics adapter is documented in
   docs/adapters/rollout_metrics.md and passes the conformance suite (Prometheus against a
   mock transport - unverified against a real Prometheus).

Everything runs in this process on temporary SQLite databases and a filesystem registry.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 7 test helpers and the import-boundary check


def _settings(tmp: Path):
    from oran_adapt.core.config import Settings
    from oran_adapt.db.migrate import upgrade_to_head

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp / "work"),
        log_json=False,
        log_level="WARNING",
        auth_enabled=False,
        notification_dispatch_enabled=False,
        registry_backend="filesystem",
        registry_fs_root=str(tmp / "registry"),
        artifact_store_root=str(tmp / "store"),
    )
    upgrade_to_head(settings.database_url)
    return settings


def _env(tmp: Path):
    from test_phase7_gate_delivery import Env

    return Env(_settings(tmp), tmp)


def marginally_worse_rejected_and_recorded(tmp: Path) -> str:
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from test_phase7_gate_delivery import Flipper, _gate

    from oran_adapt.api.app import create_app
    from oran_adapt.core.enums import AuditAction
    from oran_adapt.db.base import session_scope
    from oran_adapt.db.models import AuditLog
    from oran_adapt.orchestrator import pipeline

    report = _gate(Flipper(0.10), Flipper(0.11), tmp)
    gate = report.gate
    assert gate is not None and gate.mode == "superiority", gate
    assert not report.passed and gate.verdict == "REJECT", gate
    env = _env(tmp)
    report = report.model_copy(update={"model_id": env.model_id})
    with session_scope(env.sf) as session:
        row = pipeline._record_gate(session, env.model_id, "1", report)
        assert row is not None
    with session_scope(env.sf) as session:
        audits = list(session.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.GATE_DECIDED)))
        assert len(audits) == 1, audits
    with TestClient(create_app(env.settings)) as client:
        listed = client.get(f"/api/v1/models/{env.model_id}/gate-decisions").json()
    assert listed[0]["verdict"] == "REJECT" and listed[0]["policy_hash"] == gate.policy_hash
    return (f"delta {gate.delta:+.4f}, 95% CI [{gate.ci_low:+.4f}, {gate.ci_high:+.4f}] vs "
            f"margin {gate.threshold}: REJECT; recorded (policy {gate.policy_hash[:12]}), "
            f"audited, listed by the API")


def clear_improvement_accepted(tmp: Path) -> str:
    from test_phase7_gate_delivery import Flipper, _gate

    report = _gate(Flipper(0.30), Flipper(0.02), tmp)
    gate = report.gate
    assert report.passed and gate is not None and gate.verdict == "ACCEPT", gate
    assert all(g.passed for g in gate.guardrails), gate.guardrails
    return (f"delta {gate.delta:+.4f}, CI low {gate.ci_low:+.4f} > {gate.threshold}: ACCEPT; "
            f"guardrails {', '.join(g.name for g in gate.guardrails)} passed")


def guardrail_breach_rejects(tmp: Path) -> str:
    import numpy as np
    from test_phase7_gate_delivery import Flipper, _frame, _gate

    from oran_adapt.core.policies import GatePolicy, SliceGuard

    X, _ = _frame()
    context = X.assign(cell=np.where(X["rsrp"] > -90, "near", "far"))
    policy = GatePolicy(slices=SliceGuard(columns=["cell"], max_drop=0.05))
    report = _gate(Flipper(0.30), Flipper(0.40, cell=0), tmp, policy, context)
    gate = report.gate
    assert gate is not None and gate.delta > 0 and not report.passed, gate
    guard = next(g for g in gate.guardrails if g.name == "slice:cell")
    assert not guard.passed, guard
    return f"better overall (delta {gate.delta:+.4f}) but {guard.detail}: REJECT"


def canary_success(tmp: Path) -> str:
    from test_phase7_gate_delivery import CANARY, T0

    env = _env(tmp)
    rid = env.start("canary", CANARY)
    percents = [env.traffic().percent]
    env.observe(rid, T0 + timedelta(seconds=20), 0.01, 0.01)
    assert env.tick(rid, T0 + timedelta(seconds=61)) == "CANARY"
    percents.append(env.traffic().percent)
    env.observe(rid, T0 + timedelta(seconds=90), 0.01, 0.01)
    assert env.tick(rid, T0 + timedelta(seconds=125)) == "PROMOTED"
    split = env.traffic()
    assert env.live() == "2" and split.candidate is None and split.stable == "2", split
    return (f"canary {' -> '.join(f'{p}%' for p in percents)} -> 100% on healthy metrics: "
            f"PROMOTED, LIVE=2, split cleared (read back)")


def canary_breach_rolls_back(tmp: Path) -> str:
    from test_phase7_gate_delivery import CANARY, T0

    env = _env(tmp)
    rid = env.start("canary", CANARY)
    env.observe(rid, T0 + timedelta(seconds=20), 0.01, 0.20)
    assert env.tick(rid, T0 + timedelta(seconds=30)) == "ROLLED_BACK"
    split = env.traffic()
    assert split.candidate is None and split.stable == "1", split
    assert env.deployer.serving(env.name) == "1" and env.live() == "1"
    return (f"candidate error_rate 0.20 vs stable 0.01: ROLLED_BACK automatically; serving "
            f"reads back {split.stable} with no candidate, LIVE={env.live()} "
            f"({env.get(rid)['reason']})")


def approval_and_expiry(tmp: Path) -> str:
    from test_phase7_gate_delivery import T0

    from oran_adapt.core.errors import RolloutStateError
    from oran_adapt.core.policies import DeliveryPolicy
    from oran_adapt.db.base import session_scope
    from oran_adapt.delivery import controller

    env = _env(tmp)
    policy = DeliveryPolicy(approval_ttl_s=600)
    rid = env.start("manual", policy)
    with session_scope(env.sf) as session:
        row = controller.approve(session, env.delivery, rid, actor="acceptance",
                                 now=T0 + timedelta(seconds=60))
        assert row.state == "PROMOTED", row.state
    assert env.live() == "2"
    env.registry.set_alias(env.name, env.settings.live_alias, "1")
    late = env.start("manual", policy)
    try:
        with session_scope(env.sf) as session:
            controller.approve(session, env.delivery, late, actor="acceptance",
                               now=T0 + timedelta(seconds=601))
    except RolloutStateError:
        pass
    else:
        raise AssertionError("an approval after the deadline was accepted")
    assert env.get(late)["state"] == "EXPIRED" and env.live() == "1"
    return "approved within 600s: PROMOTED; approval at 601s refused, rollout EXPIRED, LIVE kept"


def boundaries_docs_conformance(tmp: Path) -> str:
    import pytest
    from test_import_boundary import violations

    from oran_adapt import plugins

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    guide = (ROOT / "docs" / "adapters" / "rollout_metrics.md").read_text(encoding="utf-8")
    undocumented = [a for a in plugins.adapters("rollout_metrics") if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/rollout_metrics.md: {undocumented}"
    tests = TESTS / "test_phase7_gate_delivery.py"
    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", str(tests), "-k",
                        "conformance"])
    assert code == 0, "the rollout metrics conformance suite failed"
    return ("import boundary clean; rollout metrics adapters documented and conformant "
            "(api, template; prometheus against a mock transport: unverified against "
            "Prometheus)")


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("marginally-worse candidate rejected, decision recorded",
     marginally_worse_rejected_and_recorded),
    ("clear improvement accepted", clear_improvement_accepted),
    ("guardrail breach rejects", guardrail_breach_rejects),
    ("canary success path promotes", canary_success),
    ("canary breach rolls back, served version verified", canary_breach_rolls_back),
    ("manual approval and its expiry", approval_and_expiry),
    ("boundaries, documentation and conformance", boundaries_docs_conformance),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase7-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 7 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
