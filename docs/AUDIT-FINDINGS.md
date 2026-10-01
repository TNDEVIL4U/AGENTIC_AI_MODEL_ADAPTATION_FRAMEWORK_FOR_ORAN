# Audit findings matrix

The production-hardening program started from ten audit findings. Each row below maps one
finding to the modules that close it, the configuration keys that select or tune the new
behaviour, and the named tests that prove it.

`scripts/audit.py` checks every row, and `scripts/verify.sh all` and `scripts/verify.sh 15`
run it. The check fails if:

- a module path does not exist;
- a key is not a `Settings` field;
- a named test does not exist;
- a named test did not pass in the gate's own test run.

A parametrised test counts as passed only if every one of its cases passed. When no fresh
report of the gate's test run holds a named test, the audit runs that test itself. Either way,
every test named here has actually run.

The phase report that closed each finding holds the full acceptance table
(`docs/PHASE<N>_REPORT.md`).

| # | Finding | Phase | Modules | Config keys | Tests |
|---|---|---|---|---|---|
| 1 | The model registry was MLflow: the domain called the MLflow client, and "registry" also meant "load a model" | 2 | `src/oran_adapt/ports/registry.py`, `src/oran_adapt/adapters/registry/`, `src/oran_adapt/conformance/registry.py` | `REGISTRY_BACKEND` | `tests/unit/test_phase2_registry.py::test_conformance`, `tests/unit/test_phase2_registry.py::test_handler_roundtrip_and_detection` |
| 2 | "Deployed" meant "the LIVE alias moved": nothing checked that anything served the new version | 3 | `src/oran_adapt/registry/deployment.py`, `src/oran_adapt/adapters/deployment/`, `src/oran_adapt/registry/promotion.py` | `DEPLOYMENT_BACKEND` | `tests/unit/test_phase3_deployment.py::test_promotion_rolls_out_and_reads_back`, `tests/unit/test_phase3_deployment.py::test_failed_rollout_restores_live_and_serving` |
| 3 | No outbound notifications: callers had to poll or block on the POST | 4 | `src/oran_adapt/notifications/dispatcher.py`, `src/oran_adapt/notifications/signing.py`, `src/oran_adapt/adapters/notify.py` | `NOTIFICATION_BACKEND`, `NOTIFICATION_SIGNING_KEYS` | `tests/unit/test_phase4_notifications.py::test_gate_scenario`, `tests/unit/test_phase4_notifications.py::test_signature_verification_rules` |
| 4 | Data arrived only as JSON rows in the request body, stored one DB row per record | 5 | `src/oran_adapt/datastore/access.py`, `src/oran_adapt/datastore/formats.py`, `src/oran_adapt/adapters/datasets.py` | `DATASET_BACKENDS`, `DATASET_MAX_ROWS` | `tests/unit/test_phase5_data_by_reference.py::test_reference_hashes_and_reads_like_inline`, `tests/unit/test_phase5_data_by_reference.py::test_limits_are_enforced_before_reading` |
| 5 | `POST /adaptation/events` blocked until the job ended, and the job ran under the API process | 6 | `src/oran_adapt/orchestrator/worker.py`, `src/oran_adapt/orchestrator/jobs.py`, `src/oran_adapt/adapters/job_queues.py` | `JOB_QUEUE_BACKEND`, `JOB_LEASE_TTL_S`, `JOB_DEADLINE_S` | `tests/unit/test_phase6_execution.py::test_expired_lease_is_requeued_and_finished_once`, `tests/unit/test_phase6_execution.py::test_running_job_is_stopped_at_its_deadline`, `tests/unit/test_phase6_execution.py::test_duplicate_event_from_two_replicas_makes_one_job` |
| 6 | The gate passed a candidate up to a tolerance worse than the incumbent, and delivery was all at once | 7 | `src/oran_adapt/validation/gate.py`, `src/oran_adapt/delivery/controller.py`, `src/oran_adapt/core/policies.py` | `GATE_POLICY`, `DELIVERY_STRATEGY`, `DELIVERY_POLICY` | `tests/unit/test_phase7_gate_delivery.py::test_marginally_worse_candidate_is_rejected_with_its_reasons`, `tests/unit/test_phase7_gate_delivery.py::test_canary_breach_rolls_back_and_the_served_version_reads_back` |
| 7 | Never packaged: no images, chart or migration job for production | 11 | `src/oran_adapt/core/liveness.py`, `src/oran_adapt/db/migrate.py`, `deploy/helm/` | `WORKER_HEALTH_FILE`, `MIGRATION_WAIT_TIMEOUT_S` | `tests/unit/test_phase11_packaging.py::test_no_manifest_holds_an_environment_specific_literal`, `tests/unit/test_phase11_packaging.py::test_the_chart_has_every_required_object` |
| 8 | The LLM assumed internet access and a configured provider | 10 | `src/oran_adapt/llm/guard.py`, `src/oran_adapt/llm/client.py`, `src/oran_adapt/adapters/llm_providers.py` | `LLM_ENABLED`, `LLM_PROVIDER`, `LLM_COST_BUDGET` | `tests/unit/test_phase10_llm.py::test_the_pipeline_runs_end_to_end_with_egress_blocked`, `tests/unit/test_phase10_llm.py::test_an_unreachable_provider_degrades_to_the_rules` |
| 9 | API keys were the only identity; no authorization matrix, no SSRF policy | 9 | `src/oran_adapt/api/security.py`, `src/oran_adapt/adapters/auth.py`, `src/oran_adapt/core/outbound.py` | `AUTH_BACKEND`, `POLICY_BACKEND`, `OUTBOUND_ALLOWLIST` | `tests/unit/test_phase9_security.py::test_every_route_has_an_explicit_policy`, `tests/unit/test_phase9_security.py::test_oidc_rejects_forged_expired_and_misdirected_tokens` |
| 10 | Framework dispatch by name tables; only sklearn-like and feed-forward torch models | 8 | `src/oran_adapt/ports/model_type.py`, `src/oran_adapt/adaptation/model_types.py`, `src/oran_adapt/adapters/model_types/` | `MODEL_TYPES` | `tests/unit/test_phase8_model_types.py::test_sequence_model_inspect_retrain_evaluate_gate`, `tests/unit/test_phase8_model_types.py::test_unsupported_model_gives_a_typed_result_with_no_stack_trace` |

Findings 7 and 9 each have one part that is unverified locally:

- Finding 7: the image builds, `helm lint` and `helm template`, and the kind/k3d install.
- Finding 9: the image and dependency CVE scans.

These run in CI; see `docs/PRODUCTION-READINESS.md`. The tests named here are the parts that
run locally.
