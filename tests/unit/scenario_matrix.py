"""The Phase 13 scenario matrix: each end-to-end scenario the framework must survive, and the
tests that prove it. test_phase13_scenarios checks the matrix is complete and every test
exists; scripts/acceptance/phase13.py runs the non-heavy ones and times them."""

from __future__ import annotations

import ast
from pathlib import Path

REQUIRED = (
    "shadow",
    "gate rejects a marginal candidate",
    "gate passes, canary, promote, verified",
    "canary breach, automatic rollback",
    "duplicate event",
    "stale version",
    "cancellation",
    "worker killed",
    "large dataset",
    "sequence model",
    "unsupported model type",
    "egress blocked",
    "dependency unavailable",
)

REQUIRED_DEPENDENCIES = (
    "database",
    "model registry",
    "artifact store",
    "job queue",
    "cdc broker",
    "rollout metrics",
    "llm provider",
    "serving system",
    "http dataset",
    "notification sink",
    "secrets (vault)",
    "oidc issuer",
)

_P1 = "tests/unit/test_phase1_foundation.py"
_P3 = "tests/unit/test_phase3_deployment.py"
_P4 = "tests/unit/test_phase4_notifications.py"
_P5 = "tests/unit/test_phase5_data_by_reference.py"
_P6 = "tests/unit/test_phase6_execution.py"
_P7 = "tests/unit/test_phase7_gate_delivery.py"
_P8 = "tests/unit/test_phase8_model_types.py"
_P10 = "tests/unit/test_phase10_hardening.py"
_LLM = "tests/unit/test_phase10_llm.py"
_C13 = "tests/unit/test_phase13_conformance.py"
_S13 = "tests/unit/test_phase13_scenarios.py"
_M1 = "tests/unit/test_phase14_member1.py"
_SC = "tests/unit/test_phase14_stage_c.py"
_R15 = "tests/unit/test_phase15_recovery.py"
_OUT = f"{_S13}::test_each_dependency_unavailable_in_turn_fails_with_its_typed_error"

# Which test takes each dependency down.
DEPENDENCIES = {
    "database": f"{_P1}::test_database_unreachable_raises_structured_error",
    "model registry": f"{_P1}::test_mlflow_unavailable_raises",
    "artifact store": f"{_C13}::test_artifact_store_conformance",
    "job queue": f"{_P6}::test_job_queue_conformance",
    "cdc broker": f"{_SC}::test_kafka_unavailable_stores_and_commits_nothing",
    "rollout metrics": f"{_P7}::test_unreachable_metrics_leave_the_rollout_unchanged",
    "llm provider": f"{_LLM}::test_an_unreachable_provider_degrades_to_the_rules",
    "serving system": f"{_P3}::test_readiness_reports_an_unreachable_serving_system",
    "http dataset": f"{_OUT}[http-dataset]",
    "notification sink": f"{_OUT}[notification-webhook]",
    "secrets (vault)": f"{_OUT}[vault]",
    "oidc issuer": f"{_OUT}[oidc-jwks]",
}

SCENARIOS: dict[str, list[str]] = {
    "shadow": [
        f"{_P7}::test_shadow_healthy_hands_over_to_approval",
        f"{_P7}::test_shadow_without_a_verdict_expires",
    ],
    "gate rejects a marginal candidate": [
        f"{_P7}::test_marginally_worse_candidate_is_rejected_with_its_reasons",
    ],
    "gate passes, canary, promote, verified": [
        f"{_P7}::test_deliver_starts_a_canary_and_reports_delivering",
        f"{_P7}::test_canary_success_walks_the_steps_and_promotes",
    ],
    "canary breach, automatic rollback": [
        f"{_P7}::test_canary_breach_rolls_back_and_the_served_version_reads_back",
    ],
    "duplicate event": [
        f"{_P6}::test_duplicate_event_from_two_replicas_makes_one_job",
        f"{_SC}::test_duplicate_cdc_events_are_idempotent",
        f"{_P10}::test_duplicate_event_is_deduplicated_without_rerunning",
    ],
    "stale version": [
        f"{_S13}::test_a_drift_event_on_a_version_that_is_no_longer_live_takes_no_action",
        f"{_P6}::test_stale_lease_holder_cannot_write",
        f"{_M1}::test_best_older_version_goes_live_without_training",
    ],
    "cancellation": [
        f"{_P6}::test_cancel_queued_job_is_immediate",
        f"{_P6}::test_cancel_stops_a_running_job_within_the_checkpoint_interval",
    ],
    "worker killed": [
        f"{_P6}::test_worker_process_lost_mid_attempt_is_requeued",
        f"{_P6}::test_expired_lease_is_requeued_and_finished_once",
        f"{_R15}::test_after_a_restart_the_orphaned_job_is_failed_and_the_model_runs_again",
        f"{_P4}::test_gate_scenario_api_killed_mid_delivery",
    ],
    "large dataset": [
        f"{_S13}::test_a_large_dataset_is_registered_and_sampled_in_bounded_memory",
        f"{_P5}::test_limits_are_enforced_before_reading",
        f"{_P5}::test_analysis_sample_is_bounded_and_spread",
    ],
    "sequence model": [
        f"{_P8}::test_sequence_model_inspect_retrain_evaluate_gate",
    ],
    "unsupported model type": [
        f"{_P8}::test_unsupported_model_gives_a_typed_result_with_no_stack_trace",
        f"{_P8}::test_unsupported_model_fails_the_job_with_the_typed_error",
    ],
    "egress blocked": [
        f"{_LLM}::test_the_pipeline_runs_end_to_end_with_egress_blocked",
    ],
    "dependency unavailable": [
        *DEPENDENCIES.values(),
        f"{_OUT}[notification-ping]",
        f"{_OUT}[serving-system]",
        f"{_P1}::test_ready_reports_503_when_database_down",
        f"{_P10}::test_transient_failure_is_retried_then_succeeds",
        f"{_P10}::test_retries_exhausted_marks_job_failed",
    ],
}


def _marked_heavy(decorators: list[ast.expr]) -> bool:
    return any(ast.unparse(d).replace(" ", "").startswith("pytest.mark.heavy")
               for d in decorators)


def is_heavy(root: Path, node_id: str) -> bool:
    """Whether ``node_id``'s test function (or its whole module) is marked heavy."""
    rel, func = node_id.split("::")
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ) and "heavy" in ast.unparse(node.value):
            return True
        if isinstance(node, ast.FunctionDef) and node.name == func.split("[")[0]:
            return _marked_heavy(node.decorator_list)
    return False


def fast_node_ids(root: Path) -> list[str]:
    """Every non-heavy test in the matrix, once, in matrix order."""
    seen: dict[str, None] = {}
    for cases in SCENARIOS.values():
        for node_id in cases:
            if not is_heavy(root, node_id):
                seen.setdefault(node_id, None)
    return list(seen)
