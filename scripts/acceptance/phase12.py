"""Hardening Phase 12 acceptance: observability.

Run by scripts/verify.sh 12 after lint, the import-boundary test and the scoped tests.

1. One drift event is one continuous trace from intake to deployment verification: intake ->
   job.publish -> job.attempt (worker) -> job.attempt.run -> one span per stage -> the adapter
   calls -> deployment.split / deployment.verify, with the attempt in the worker's thread and
   in a fresh process; the trace id is derived from the event and the traceparent is stored on
   the job (migration 0011).
2. Metrics: every adapter the composition root builds (not the job executor) records latency
   and errors by port, adapter, operation and error code; every exported metric's labels are
   bounded (no job, model or event ids).
3. Logs carry trace and span ids and redact credentials.
4. Alert rules as code validate and read only exported metrics; every alert maps to a runbook
   with Fires when / Impact / Check / Fix, and every runbook to an alert.
5. Dashboards as code validate, query only exported metrics and cover the spec's signals; the
   chart ships them and scrapes the workers' metrics port.
6. Tracing is off by default and its keys are validated.
7. What the laptop cannot run: export to a real OTLP collector, promtool against the rule file
   (wired into CI's packaging job), Grafana loading the dashboards. Unverified locally.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gate import run_selection

PHASE12 = TESTS / "test_phase12_observability.py"


def _run(selection: str) -> None:
    run_selection(PHASE12, selection)


def one_trace(tmp: Path) -> str:
    _run("one_drift_event_is_one_trace or trace_id_is_derived or migration_0011")
    return ("intake -> publish -> attempt -> stages -> adapter calls -> deployment.verify; "
            "thread and process; id = sha256(key)[:128 bits]")


def metrics(tmp: Path) -> str:
    _run("observed_adapters or bootstrap_instruments or bounded_labels")
    return "adapter latency/errors by port, adapter, operation, code; labels bounded"


def logs(tmp: Path) -> str:
    _run("logs_carry_trace_ids")
    return "trace_id/span_id on every line in a span; URL creds, key=value secrets, bearer masked"


def alerts_and_runbooks(tmp: Path) -> str:
    _run("alert_rules_validate or every_alert_rule_maps")
    doc = yaml.safe_load((ROOT / "deploy/helm/oran-adapt/files/prometheus-rules.yaml")
                         .read_text(encoding="utf-8"))
    alerts = [rule["alert"] for group in doc["groups"] for rule in group["rules"]]
    return f"{len(alerts)} alerts, {len(alerts)} runbooks, 1:1"


def dashboards(tmp: Path) -> str:
    _run("dashboards_validate or chart_ships_the_dashboards")
    files = sorted((ROOT / "deploy/helm/oran-adapt/files/dashboards").glob("*.json"))
    return f"{', '.join(p.name for p in files)}; ConfigMap + PodMonitor in the chart"


def tracing_config(tmp: Path) -> str:
    _run("without_an_exporter or tracing_keys_are_validated")
    return "TRACING_EXPORTER=none by default (no-op spans); jsonl/otlp need their keys"


def ci_wiring(tmp: Path) -> str:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    runs = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["packaging"]["steps"])
    assert "promtool" in runs and "check rules" in runs, "packaging job lacks promtool"
    return "promtool check rules in CI's packaging job (run in CI)"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("one drift event, one continuous trace", one_trace),
    ("adapter metrics, bounded labels", metrics),
    ("structured logs: trace ids, no secrets", logs),
    ("alert rules as code, a runbook per alert", alerts_and_runbooks),
    ("dashboards as code", dashboards),
    ("tracing off by default, keys validated", tracing_config),
    ("CI runs what the laptop cannot", ci_wiring),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase12-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 12 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
