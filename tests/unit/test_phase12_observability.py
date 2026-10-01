"""Hardening Phase 12: observability.

One drift event is one trace, from the intake span to the deployment read-back, across the
broker hop, the worker, the attempt (in the worker's process or a fresh one) and the rollout
ticks after it. Metrics carry only bounded labels; every alert rule has a runbook and every
dashboard panel queries a metric the code exports; logs carry correlation and trace ids and no
secrets.
"""

from __future__ import annotations

import json
import logging
import pickle
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import inspect

from oran_adapt import bootstrap
from oran_adapt.adapters import tracing as tracing_sdk
from oran_adapt.adapters.job_queues import DatabaseQueue
from oran_adapt.core import metrics, observed, tracing
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import JobStatus
from oran_adapt.core.errors import ConfigurationError, RegistryUnavailableError
from oran_adapt.core.logging import JsonFormatter, TraceIdFilter, redact
from oran_adapt.core.policies import DeliveryPolicy, HealthRule
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.migrate import downgrade_to_base, upgrade_to_head
from oran_adapt.db.models import ModelMetadata
from oran_adapt.orchestrator import jobs as jobs_module
from oran_adapt.orchestrator import pipeline
from oran_adapt.orchestrator.context import report_stage
from oran_adapt.orchestrator.jobs import submit_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.orchestrator.worker import Worker

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "oran-adapt"
RULES = CHART / "files" / "prometheus-rules.yaml"
DASHBOARDS = CHART / "files" / "dashboards"
RUNBOOKS = ROOT / "docs" / "runbooks"
CANARY = DeliveryPolicy(
    min_samples=10, canary_steps=[10, 50, 100], canary_step_hold_s=60,
    health=[HealthRule(metric="error_rate", max_degradation=0.01)],
)


def _delivering_pipeline(session, event, settings, *, registry, llm_client, workdir):
    """Stands in for the adaptation: scores, reuses version 2 and delivers it as a canary with
    the configured serving system, which reads the first split back."""
    report_stage(JobStatus.EVALUATING_VERSIONS, "scoring registered versions")
    report_stage(JobStatus.REUSE_DECISION, "version 2 is better")
    deployer = bootstrap.build_deployer(settings, registry)
    return pipeline._deliver(
        session, settings, registry, deployer, workdir, model_id=event.model_id,
        live_version="1", new_version="2", gate_row=None,
        result=JobResult(model_id=event.model_id, outcome="DELIVERING", reason=""),
    )


@pytest.fixture
def traced(migrated_settings, tmp_path):
    spans = tmp_path / "spans.jsonl"
    settings = migrated_settings.model_copy(update={
        "registry_backend": "filesystem",
        "registry_fs_root": str(tmp_path / "registry"),
        "artifact_store_root": str(tmp_path / "store"),
        "deployment_backend": "registry-alias",
        "deployment_timeout_s": 5.0,
        "deployment_poll_s": 0.01,
        "delivery_strategy": "canary",
        "delivery_policy": CANARY,
        "job_queue_backend": "database",
        "job_heartbeat_s": 0.1,
        "tracing_exporter": "jsonl",
        "tracing_jsonl_path": str(spans),
    })
    registry = bootstrap.build_registry(settings)
    name, model_id = "cell_trace", "cell-trace"
    for label in ("a", "b"):
        path = tmp_path / "artifacts" / label
        path.mkdir(parents=True)
        (path / "model.onnx").write_text(label, encoding="utf-8")
        registry.create_version(name, str(path))
    registry.set_alias(name, settings.live_alias, "1")
    sf = make_session_factory(create_db_engine(settings.database_url))
    with session_scope(sf) as session:
        session.add(ModelMetadata(model_id=model_id, mlflow_model_name=name))
    bootstrap.configure_tracing(settings)
    yield settings, sf, registry, model_id, spans
    tracing.set_provider(None)
    tracing_sdk._configured = None


def _spans(path: Path) -> list[dict[str, Any]]:
    tracing.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_one_trace(spans: list[dict[str, Any]], event_id: str) -> dict[str, dict]:
    trace_id = tracing.trace_id_hex(event_id)
    assert {s["trace_id"] for s in spans} == {trace_id}
    by_id = {s["span_id"]: s for s in spans}
    roots = [s for s in spans if s["parent_span_id"] is None]
    assert roots and {r["name"] for r in roots} == {"intake"}  # one per submission
    for s in spans:  # every parent is in the trace: nothing is cut off
        assert s["parent_span_id"] is None or s["parent_span_id"] in by_id, s["name"]
    return by_id


def _ancestors(span: dict, by_id: dict[str, dict]) -> list[str]:
    names = []
    while span["parent_span_id"] is not None:
        span = by_id[span["parent_span_id"]]
        names.append(span["name"])
    return names


@pytest.mark.parametrize("mode", ["thread", "process"])
def test_one_drift_event_is_one_trace_from_intake_to_deployment_verification(
        traced, monkeypatch, mode) -> None:
    settings, sf, registry, model_id, spans_path = traced
    settings = settings.model_copy(update={"job_execution_mode": mode})
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _delivering_pipeline)
    event = DriftEvent(model_id=model_id, event_id=f"evt-trace-{mode}")
    queued = submit_adaptation_job(sf, event, settings, registry=registry, llm_client=None,
                                   workdir=settings.artifact_workdir, queue=DatabaseQueue())
    assert queued.status == JobStatus.QUEUED
    # The same event again joins the same trace (and returns the same job).
    again = submit_adaptation_job(sf, event, settings, registry=registry, llm_client=None,
                                  workdir=settings.artifact_workdir, queue=DatabaseQueue())
    assert again.duplicate and again.job_id == queued.job_id

    worker = Worker(sf, settings, registry=registry, llm_client=None,
                    workdir=settings.artifact_workdir, queue=DatabaseQueue())
    assert worker.run(once=True) == 1
    assert worker.tick_rollouts()  # a later controller pass continues the job's trace

    spans = _spans(spans_path)
    by_id = _assert_one_trace(spans, event.event_id or "")
    names = [s["name"] for s in spans]
    for expected in ("intake", "job.publish", "job.attempt", "job.attempt.run",
                     "stage DATA_PREPARING", "stage EVALUATING_VERSIONS", "stage PROMOTING",
                     "deployment.split", "deployment.verify", "rollout.tick"):
        assert expected in names, (expected, sorted(set(names)))
    assert names.count("intake") == 2  # the duplicate submission, in the same trace
    verify = next(s for s in spans if s["name"] == "deployment.verify")
    chain = _ancestors(verify, by_id)
    assert chain[:3] == ["deployment.split", "stage PROMOTING", "job.attempt.run"]
    assert chain[-2:] == ["job.attempt", "intake"]
    # Adapter calls are spans inside the stage that made them.
    status = [s for s in spans if s["name"] == "deployment.status"]
    assert status and all("deployment.verify" in _ancestors(s, by_id) for s in status)
    attempt_run = next(s for s in spans if s["name"] == "job.attempt.run")
    attempt = next(s for s in spans if s["name"] == "job.attempt")
    assert (attempt_run["pid"] != attempt["pid"]) is (mode == "process")
    assert attempt["kind"] == "CONSUMER"
    assert next(s for s in spans if s["name"] == "job.publish")["kind"] == "PRODUCER"


def test_the_trace_id_is_derived_from_the_event_and_stored_on_the_job(traced, monkeypatch):
    settings, sf, registry, model_id, spans_path = traced
    event = DriftEvent(model_id=model_id)  # no event_id: keyed on the idempotency key
    queued = submit_adaptation_job(sf, event, settings, registry=registry, llm_client=None,
                                   workdir=settings.artifact_workdir, queue=DatabaseQueue())
    job = jobs_module.load_job
    with session_scope(sf) as session:
        row = job(session, queued.job_id)
        traceparent = row.trace_context
    trace_id = tracing.trace_id_hex(event.idempotency_key())
    assert traceparent and traceparent.split("-")[1] == trace_id
    assert {s["trace_id"] for s in _spans(spans_path)} == {trace_id}


def test_without_an_exporter_nothing_is_recorded_and_jobs_still_run(migrated_settings) -> None:
    assert bootstrap.configure_tracing(migrated_settings) is False
    assert tracing.inject() is None
    with tracing.event_span("intake", "k"):
        assert tracing.inject() is None and tracing.current_ids() is None


def test_tracing_keys_are_validated() -> None:
    with pytest.raises(ConfigurationError, match="TRACING_JSONL_PATH"):
        Settings(_env_file=None, tracing_exporter="jsonl")
    with pytest.raises(ConfigurationError, match="TRACING_OTLP_ENDPOINT"):
        Settings(_env_file=None, tracing_exporter="otlp")
    otlp = Settings(_env_file=None, tracing_exporter="otlp",
                    tracing_otlp_endpoint="http://collector:4318/v1/traces")
    try:
        import opentelemetry.exporter.otlp.proto.http.trace_exporter  # noqa: F401
    except ImportError:
        with pytest.raises(ConfigurationError, match="opentelemetry-exporter-otlp"):
            tracing_sdk.configure(otlp)


def test_migration_0011_up_and_down(settings) -> None:
    upgrade_to_head(settings.database_url)
    engine = create_db_engine(settings.database_url)
    assert "trace_context" in {c["name"] for c in inspect(engine).get_columns("adaptation_job")}
    engine.dispose()
    downgrade_to_base(settings.database_url)
    upgrade_to_head(settings.database_url)


# ---- adapter instrumentation -------------------------------------------------------------------


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, x: int) -> int:
        self.calls += 1
        return self.inner(x)

    def inner(self, x: int) -> int:
        return x * 2

    def fail(self) -> None:
        raise RegistryUnavailableError("down")


def _sample(name: str, **labels: str) -> float:
    value = metrics.REGISTRY.get_sample_value(name, labels)
    return value or 0.0


def test_observed_adapters_keep_their_type_pickle_and_count_errors() -> None:
    adapter = observed.observe(_Adapter(), port="registry", name="probe")
    assert isinstance(adapter, _Adapter)
    assert observed.observe(adapter, port="registry", name="probe") is adapter
    before = _sample("adapter_call_duration_seconds_count", port="registry", adapter="probe",
                     operation="get")
    inner_before = _sample("adapter_call_duration_seconds_count", port="registry",
                           adapter="probe", operation="inner")
    assert adapter.get(2) == 4
    assert _sample("adapter_call_duration_seconds_count", port="registry", adapter="probe",
                   operation="get") == before + 1
    # A call the adapter makes to itself is not counted again.
    assert _sample("adapter_call_duration_seconds_count", port="registry", adapter="probe",
                   operation="inner") == inner_before
    errors = _sample("adapter_errors_total", port="registry", adapter="probe", operation="fail",
                     code="MLFLOW_UNAVAILABLE")
    with pytest.raises(RegistryUnavailableError):
        adapter.fail()
    assert _sample("adapter_errors_total", port="registry", adapter="probe", operation="fail",
                   code="MLFLOW_UNAVAILABLE") == errors + 1
    copy = pickle.loads(pickle.dumps(adapter))
    assert copy.get(3) == 6 and copy.calls == 2


def test_bootstrap_instruments_every_port_but_the_executor(migrated_settings, tmp_path) -> None:
    settings = migrated_settings.model_copy(update={
        "registry_backend": "filesystem", "registry_fs_root": str(tmp_path / "r")})
    registry = bootstrap.build_registry(settings)
    assert getattr(registry, observed._ATTR, False)
    assert getattr(bootstrap.build_job_queue(settings), observed._ATTR, False)
    assert not getattr(bootstrap.build_job_executor(settings), observed._ATTR, False)
    assert "job_executor" in bootstrap.UNOBSERVED_PORTS


# ---- logs --------------------------------------------------------------------------------------


def test_logs_carry_trace_ids_and_no_secrets(traced) -> None:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                               "retrying postgresql://app:hunter2@db/x with token=abc123", None,  # secret-scan: allow
                               None)
    with tracing.event_span("intake", "log-key"):
        TraceIdFilter().filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert line["trace_id"] == tracing.trace_id_hex("log-key") and len(line["span_id"]) == 16
    assert "hunter2" not in line["message"] and "abc123" not in line["message"]
    assert line["message"] == "retrying postgresql://***:***@db/x with token=***"
    assert redact("Authorization: Bearer eyJhbGciOi.x.y") == "Authorization: Bearer ***"
    assert redact("lease_token=keep input_tokens=12") == "lease_token=keep input_tokens=12"


# ---- metrics, alert rules, runbooks, dashboards -----------------------------------------------

# Labels whose values an operator or the code bounds (classes, states, reasons, adapters).
_UNBOUNDED = {"job_id", "event_id", "model_id", "rollout_id", "trace_id", "span_id", "tenant",
              "correlation_id", "version", "idempotency_key", "user", "url", "path_params"}


def _exported() -> dict[str, tuple[str, ...]]:
    families: dict[str, tuple[str, ...]] = {}
    for name in dir(metrics):
        metric = getattr(metrics, name)
        if hasattr(metric, "_labelnames") and hasattr(metric, "describe"):
            for family in metric.describe():
                families[family.name] = tuple(metric._labelnames)
    return families


def test_every_metric_has_bounded_labels() -> None:
    for name, labels in _exported().items():
        assert not set(labels) & _UNBOUNDED, (name, labels)


def _referenced_metrics(expr: str) -> set[str]:
    words = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", expr))
    return {w for w in words if w.startswith(tuple(_PREFIXES))}


_PREFIXES = ("adaptation_", "adapter_", "job_", "rollout", "delivery_", "llm_", "notification",
             "worker_", "gate_", "dataset_", "deployment", "traffic_", "http_", "model_",
             "cdc_", "promotion", "rollback", "sandbox_", "drift_", "strategy_", "fine_tune",
             "retrain", "validation_")


def _metric_names() -> set[str]:
    names: set[str] = set()
    for base in _exported():
        names |= {base, f"{base}_total", f"{base}_bucket", f"{base}_count", f"{base}_sum"}
    return names


def _rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_alert_rules_validate_and_query_exported_metrics() -> None:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    known = _metric_names() | {"up"}
    labels = {"le", "job", "instance", "namespace", "pod"}
    for fam in _exported().values():
        labels |= set(fam)
    for group in doc["groups"]:
        assert group["name"] and group["rules"]
        for rule in group["rules"]:
            assert set(rule) <= {"alert", "expr", "for", "labels", "annotations"}, rule
            assert re.fullmatch(r"OranAdapt[A-Z][A-Za-z]+", rule["alert"])
            assert rule["labels"]["severity"] in {"critical", "warning", "info"}
            assert {"summary", "description", "runbook"} <= set(rule["annotations"])
            assert re.fullmatch(r"\d+[smh]", rule.get("for", "0s"))
            words = _referenced_metrics(rule["expr"])
            # Prometheus's own scrape-health series `up` is the only non-oran-adapt metric.
            assert words or re.search(r"\bup\{", rule["expr"]), rule["alert"]
            unknown = {w for w in words if w not in known and w not in labels}
            assert not unknown, (rule["alert"], unknown)


def test_every_alert_rule_maps_to_a_runbook() -> None:
    alerts = [rule["alert"] for rule in _rules()]
    assert len(alerts) == len(set(alerts))
    for rule in _rules():
        runbook = rule["annotations"]["runbook"]
        assert runbook == f"docs/runbooks/{rule['alert']}.md"
        text = (ROOT / runbook).read_text(encoding="utf-8")
        for heading in ("**Fires when**", "**Impact.**", "**Check.**", "**Fix.**"):
            assert heading in text, (runbook, heading)
    assert {p.stem for p in RUNBOOKS.glob("OranAdapt*.md")} == set(alerts)


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for panel in dashboard.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


def test_dashboards_validate_and_query_exported_metrics() -> None:
    files = sorted(DASHBOARDS.glob("*.json"))
    assert files
    known = _metric_names()
    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        assert dashboard["uid"] and dashboard["title"] and dashboard["schemaVersion"] >= 36
        ids = [p["id"] for p in _panels(dashboard)]
        assert len(ids) == len(set(ids))
        queried: set[str] = set()
        for panel in _panels(dashboard):
            if panel["type"] == "row":
                continue
            assert panel["title"] and panel["targets"] and panel["gridPos"]
            for target in panel["targets"]:
                words = _referenced_metrics(target["expr"])
                queried |= {w for w in words if w in known}
                bad = {w for w in words if w not in known and w not in _label_names()}
                assert not bad, (path.name, panel["title"], bad)
        # The spec's signals each have a panel.
        for required in ("adaptation_stage_duration_seconds_bucket", "adaptation_jobs_total",
                         "job_queue_depth", "job_queue_oldest_age_seconds",
                         "adapter_call_duration_seconds_bucket", "adapter_errors_total",
                         "rollout_steps_total", "rollouts_finished_total",
                         "delivery_failures_total", "llm_cost_total"):
            assert required in queried, (path.name, required)


def _label_names() -> set[str]:
    labels = {"le", "job", "instance", "namespace", "pod"}
    for fam in _exported().values():
        labels |= set(fam)
    return labels


def test_the_chart_ships_the_dashboards_and_scrapes_the_workers() -> None:
    monitoring = (CHART / "templates" / "monitoring.yaml").read_text(encoding="utf-8")
    assert "files/dashboards/*.json" in monitoring and "PodMonitor" in monitoring
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "dashboards" in values["metrics"] and "podMonitor" in values["metrics"]
    assert values["worker"]["metricsPort"]
