# Hardening Phase 12 report: observability

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled. What ran for real
and what used doubles:
- **Ran for real:**
  - the OpenTelemetry SDK (1.44.0) with the `jsonl` exporter: one drift event submitted through
    `submit_adaptation_job`, queued on the database queue, claimed by a real `Worker` and run
    both in the worker's thread and in a fresh process, through the filesystem registry, the
    registry-alias deployment backend and a canary first split, and the recorded spans read
    back from the file;
  - migration 0011 up and down on SQLite;
  - `core.observed` on real adapters (latency, error codes, pickling), the composition root's
    instrumentation of every port, the log formatter and its redaction;
  - static validation of the alert rules, the dashboard JSON, the runbooks and the chart
    templates against the metrics the code actually exports.
- **Doubles:** the adaptation pipeline in the trace test is a stand-in that reports two stages
  and then calls the real delivery code (`pipeline._deliver`), so the trace covers intake to
  deployment verification without training a model. The failing registry in the observed-adapter
  test is a subclass that raises `RegistryUnavailableError`.

**Unverified locally** (no collector, Prometheus, Grafana or cluster on the laptop, and none may
be installed):
- export over OTLP (`TRACING_EXPORTER=otlp` needs the `otlp` extra, which is not installed);
- `promtool check rules` on the rule file (CI job `packaging`);
- Grafana importing the dashboard and the sidecar picking up its ConfigMap;
- the PodMonitor and NetworkPolicy admitting Prometheus to the workers' port in a cluster, and
  the compose Prometheus scraping `worker:9100`.

## 1. Findings closed

| Finding | What changed |
|---|---|
| No tracing: a job's path through API, broker, worker, stages and serving could only be followed through logs | OpenTelemetry API in core (`core/tracing.py`), SDK and exporters at the composition root (`adapters/tracing.py`). One trace per drift event with a trace id derived from the event, propagated through the broker and the job row |
| Worker metrics were never scraped (found in this phase) | The job, queue, stage and rollout metrics live only in the worker processes, but only the API's `/api/v1/metrics` was scraped, so `OranAdaptJobQueueStalled` could never fire. Workers now serve `WORKER_METRICS_PORT`; the chart adds a PodMonitor and a NetworkPolicy, kustomize a port and a production policy, compose a scrape target |
| No per-stage or per-adapter signal | `adaptation_stage_duration_seconds`, `adapter_call_duration_seconds`, `adapter_errors_total`, `rollout_steps_total`, `delivery_failures_total`, `worker_up`; forwarded from attempt processes like the other job metrics |
| Logs could carry credentials | `core.logging.redact`: URL credentials, secret-named assignments and bearer tokens become `***` in the JSON and plain formats; lines carry `trace_id`/`span_id` |
| Alerts without runbooks for half the signals | 12 alert rules, 12 runbooks, checked 1:1 both ways |

## 2. Ports and adapters

No new port. Tracing is split like every other vendor dependency: core uses only the
OpenTelemetry API (a no-op until configured); the SDK, the sampler and the exporters (`console`,
`jsonl`, `otlp`) are in `oran_adapt.adapters.tracing` and installed by
`bootstrap.configure_tracing`. `core.observed` wraps every adapter the composition root builds
(all ports but the job executor, whose calls are the job itself).

## 3. Configuration keys

| Key | Default | Meaning |
|---|---|---|
| `TRACING_EXPORTER` | `none` | `none`, `console`, `jsonl`, `otlp` |
| `TRACING_SERVICE_NAME` | `oran-adapt` | `service.name` |
| `TRACING_SAMPLE_RATIO` | 1.0 | parent-based ratio sampler |
| `TRACING_JSONL_PATH` | unset | required with `jsonl` |
| `TRACING_OTLP_ENDPOINT` | unset | required with `otlp` |
| `TRACING_OTLP_TIMEOUT_S` | 10 | export timeout |
| `WORKER_METRICS_PORT` | unset (none) | a worker's `/metrics` port |
| `WORKER_METRICS_ADDR` | empty (every interface) | its bind address |
| Helm `worker.metricsPort` | 9100 | sets `WORKER_METRICS_PORT` and the container port |
| Helm `metrics.podMonitor.*` | disabled | scrapes the workers |
| Helm `metrics.dashboards.*` | disabled, label `grafana_dashboard: "1"` | dashboards ConfigMap |

`docs/operations/observability.md` lists every metric and span.

## 4. Acceptance criteria

| Criterion (spec) | Evidence | Status |
|---|---|---|
| Metrics: stage durations, outcomes by reason, queue depth and age, adapter latency/errors, gate verdicts, canary steps, rollbacks, delivery failures, dataset volume, LLM cost | `core/metrics.py`; the dashboard queries each (`test_dashboards_validate_and_query_exported_metrics`) | passed |
| Bounded label cardinality | `test_every_metric_has_bounded_labels` (no job, model, event, version or trace ids as labels) | passed |
| OTel traces propagated through the broker keyed on event_id/job_id | `test_one_drift_event_is_one_trace_from_intake_to_deployment_verification[thread, process]`, `test_the_trace_id_is_derived_from_the_event_and_stored_on_the_job` | passed |
| One drift event produces one continuous trace from intake to deployment verification | same test: one trace id; `deployment.verify` → `deployment.split` → `stage PROMOTING` → `job.attempt.run` → ... → `job.attempt` → `intake`; `rollout.tick` in the same trace | passed |
| Structured logs with correlation ids and no secrets | `test_logs_carry_trace_ids_and_no_secrets` | passed |
| Dashboards and alert rules as code; the files validate | `test_alert_rules_validate_and_query_exported_metrics`, `test_dashboards_validate_...`, `test_the_chart_ships_the_dashboards_and_scrapes_the_workers`; promtool in CI | passed; promtool unverified locally |
| A runbook per alert; a test asserts every rule maps to one | `test_every_alert_rule_maps_to_a_runbook` | passed |

## 5. Hardcoding

No category count changes (A 0, B 0, C 7, D 5). New literals, and why they are not keys:

| Where | Value | Why it is not a key |
|---|---|---|
| `core/tracing.py` | `"oran-adapt:"` trace-id salt, span names | the trace-id derivation is a contract (an operator computes it from an event); span names are the documented tree |
| `files/prometheus-rules.yaml` | thresholds and `for:` windows | alerts as code, each explained in its runbook; copy the file to change them |
| `files/dashboards/*.json` | panel layout | a dashboard is data |
| `values.yaml`, kustomize base, compose, `prometheus.yml` | worker metrics port 9100 | a default, `worker.metricsPort` in Helm and a patchable field elsewhere |

## 6. Assumptions and defaults

- Tracing is off by default; `none` costs one no-op span object per call site.
- A job queued before migration 0011 has no stored traceparent; its spans still land in the
  event's keyed trace as a new root.
- Gauges are not forwarded from an attempt process; the queue gauges are set by the worker
  loop itself.
- A worker killed on a timeout reports no metric delta for that attempt (unchanged from Phase 6).
- The Grafana sidecar discovers dashboards by the `grafana_dashboard` label (kube-prometheus
  default); change `metrics.dashboards.label` otherwise.

## 7. Unverified locally

Everything in the header's list.

## 8. Gate

`bash scripts/verify.sh 12`: **PASS in 160 s** (budget 300 s). The log is in the session's
scratchpad (`verify12.log`).

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean (mypy: 177 files) |
| 2 import boundary | 1 s | 2 passed |
| 3 no-gaps lint | 10 s | clean |
| 4 scoped tests (core.logging, orchestrator.jobs, orchestrator.worker, delivery.controller, registry.deployment; 9 files) | 11 s | 283 passed in 88 s |
| 4 smoke tier (files not run above) | 107 s | 219 passed in 44 s |
| 5 acceptance (`scripts/acceptance/phase12.py`) | 159 s | 7/7 passed (recorded results) |
