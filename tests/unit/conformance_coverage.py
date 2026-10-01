"""Which test runs each installed adapter through its port's conformance suite (Hardening
Phase 13). Registering an adapter (an entry point under ``oran_adapt.<port>``) without an entry
here fails ``test_phase13_conformance::test_every_installed_adapter_is_conformance_tested``, so
an adapter cannot ship without passing its suite; ``scripts/acceptance/phase13.py`` runs every
test named here and requires each adapter's cases to pass.

``SUITES``: port -> the conformance module that defines its rules.
``COVERAGE``: port -> (test file, test function, {adapter: parameter id or None}). A parameter
id selects that adapter's cases (``test_conformance[mirror-protocol]``); None means the one
test runs every adapter of the port. ``EXEMPT``: (port, adapter) -> why, who owns closing it and
when the exemption expires; an expired one fails the gate.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple


class Exemption(NamedTuple):
    reason: str
    owner: str
    expires: date


SUITES: dict[str, str] = {
    "registry": "oran_adapt.conformance.registry",
    "artifact_store": "oran_adapt.conformance.artifact_store",
    "model_handler": "oran_adapt.conformance.model_handler",
    "model_type": "oran_adapt.conformance.model_types",
    "deployment": "oran_adapt.conformance.deployment",
    "dataset": "oran_adapt.conformance.dataset",
    "cdc_source": "oran_adapt.conformance.cdc_source",
    "job_executor": "oran_adapt.conformance.job_executor",
    "job_queue": "oran_adapt.conformance.job_queue",
    "notification": "oran_adapt.conformance.notification",
    "llm": "oran_adapt.conformance.llm",
    "rollout_metrics": "oran_adapt.conformance.rollout_metrics",
    "auth": "oran_adapt.conformance.auth",
    "policy": "oran_adapt.conformance.policy",
    "secrets": "oran_adapt.conformance.auth",
}

_P13 = "test_phase13_conformance.py"

COVERAGE: dict[str, list[tuple[str, str, dict[str, str | None]]]] = {
    "registry": [("test_phase2_registry.py", "test_conformance", {
        "filesystem": "filesystem", "fsspec": "fsspec", "mlflow": "mlflow", "mirror": "mirror",
        "sagemaker": "sagemaker-emulator", "vertex": "vertex-emulator"})],
    "artifact_store": [(_P13, "test_artifact_store_conformance", {
        "filesystem": "filesystem", "fsspec": "fsspec"})],
    "model_handler": [(_P13, "test_model_handler_conformance", {
        "native": "native", "mlflow-flavors": "mlflow-flavors"})],
    "model_type": [("test_phase8_model_types.py", "test_builtin_model_type_conformance", {
        name: name for name in ("catboost", "keras", "lightgbm", "onnx", "sklearn",
                                "statsmodels", "torch", "torch-sequence", "xgboost")})],
    "deployment": [("test_phase3_deployment.py", "test_conformance", {
        name: name for name in ("registry-alias", "webhook", "bentoml", "gitops", "triton",
                                "kserve", "seldon", "k8s", "sagemaker", "vertex")})],
    "dataset": [("test_phase5_data_by_reference.py", "test_dataset_adapter_conformance", {
        name: name for name in ("file", "fsspec", "http", "gcs", "s3")})],
    "cdc_source": [(_P13, "test_cdc_source_conformance", {
        "polling": "polling", "kafka": "kafka"})],
    "job_executor": [(_P13, "test_job_executor_conformance", {
        "thread": "thread", "process": "process"})],
    "job_queue": [("test_phase6_execution.py", "test_job_queue_conformance", {
        name: name for name in ("database", "inline", "celery", "rq", "kubernetes")})],
    "notification": [("test_phase4_notifications.py", "test_conformance", {
        name: name for name in ("log", "webhook", "slack", "pagerduty", "pubsub", "kafka",
                                "sqs", "sns", "nats", "email")})],
    "llm": [("test_phase10_llm.py", "test_provider_adapter_conformance", {
        name: name for name in ("anthropic", "gemini", "openai-compatible")})],
    "rollout_metrics": [
        ("test_phase7_gate_delivery.py", "test_api_source_conformance", {"api": None}),
        ("test_phase7_gate_delivery.py", "test_prometheus_source_conformance",
         {"prometheus": None}),
    ],
    "auth": [("test_phase9_security.py", "test_auth_adapters_pass_the_conformance_suite", {
        name: None for name in ("api-key", "oidc", "gateway", "mtls")})],
    "policy": [(_P13, "test_policy_conformance", {"static-rbac": None}),
               ("test_phase14_integration.py", "test_opa_policy_conformance", {"opa": None})],
    "secrets": [("test_phase9_security.py", "test_secrets_adapters_pass_the_conformance_suite", {
        name: None for name in ("env", "file", "vault")})],
}

EXEMPT: dict[tuple[str, str], Exemption] = {}


def covering(port: str, adapter: str) -> list[tuple[str, str, str | None]]:
    """(test file, test function, parameter id or None) for every test that runs ``adapter``."""
    return [(path, func, adapters[adapter]) for path, func, adapters in COVERAGE.get(port, [])
            if adapter in adapters]


def case_matches(name: str, func: str, param: str | None) -> bool:
    """Whether the JUnit test case ``name`` is ``func`` run for the adapter ``param``."""
    if param is None:
        return name == func or name.startswith(func + "[")
    if not name.startswith(func + "["):
        return False
    ids = name[len(func):]
    return any(marker in ids for marker in (f"[{param}]", f"[{param}-", f"-{param}]",
                                           f"-{param}-"))
