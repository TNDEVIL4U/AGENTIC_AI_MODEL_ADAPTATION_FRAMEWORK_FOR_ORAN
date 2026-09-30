# Observability

Metrics, traces and logs of the API and the workers; dashboards, alert rules and runbooks as
code. Everything here is optional to consume: with nothing configured the API still serves
`/api/v1/metrics`, logs are JSON on stderr and spans are no-ops.

## Metrics

**Where.** The API serves `/api/v1/metrics` (`METRICS_PUBLIC`: no API key needed while true).
Job execution happens in the workers, which are separate processes. The job, queue, stage,
adapter, gate, rollout, dataset and LLM metrics are therefore only in the workers, and a worker
serves them on its own port when `WORKER_METRICS_PORT` is set (`WORKER_METRICS_ADDR`: the bind
address, empty for every interface).

| Deployment | API | Workers |
|---|---|---|
| Helm | ServiceMonitor (`metrics.serviceMonitor`) | PodMonitor (`metrics.podMonitor`), port `worker.metricsPort` |
| kustomize | the `http` port | container port `metrics` (9100); production overlay admits the `monitoring` namespace |
| compose | `api:8000` in `deploy/prometheus/prometheus.yml` | `worker:9100`, same file |

**Signals.** Each metric's label values are bounded: ports, adapter names, stages, states,
strategies, error codes and reasons, never job, model or event ids (a test checks this).

| Signal | Metric | Labels |
|---|---|---|
| Stage durations | `adaptation_stage_duration_seconds` | stage, outcome |
| Outcomes | `adaptation_jobs_total`; `adaptation_failure_total` | status, outcome; reason (error code) |
| Queue depth and age | `job_queue_depth`; `job_queue_oldest_age_seconds` | worker_class |
| Queue health | `job_requeues_total`; `job_quarantined_total`; `job_timeouts_total` | reason; -; stage |
| Adapter latency and errors | `adapter_call_duration_seconds`; `adapter_errors_total` | port, adapter, operation (, code) |
| Gate verdicts | `gate_decisions_total` | verdict, mode |
| Canary steps | `rollout_steps_total`; `rollouts_started_total`; `rollouts_active` | strategy, state |
| Rollbacks | `rollouts_finished_total{state="ROLLED_BACK"}`; `rollback_total` | strategy, state; trigger |
| Delivery failures | `delivery_failures_total` | strategy, reason (`first_split`, `serving`, `metrics`, `promotion`) |
| Dataset volume | `dataset_rows_read_total`; `dataset_bytes_read_total`; `dataset_read_duration_seconds` | storage; scheme; storage |
| LLM cost | `llm_cost_total`; `llm_tokens_total`; `llm_calls_refused_total`; `llm_fallbacks_total` | provider (, direction, reason) |
| Notifications | `notification_backlog`; `notification_deliveries_total`; `notification_circuit_open` | status; sink, outcome; sink |
| Liveness | `worker_up` | worker_class |
| HTTP | `http_requests_total`; `http_request_duration_seconds` | method, route (template), status |

Every adapter the composition root builds, except the job executor, is wrapped by
`core.observed`: each public call records `adapter_call_duration_seconds` and, when it raises,
`adapter_errors_total` with the error's code (`UNEXPECTED` for an untranslated exception).

## Traces

**One trace per drift event.** The trace id of an event is computed from the event:
the first 128 bits of `sha256("oran-adapt:" + key)`, where `key` is the event's `event_id`, or
its idempotency key when the caller gave no `event_id`. A duplicate submission lands in the
same trace. To find the trace of an event:

```python
from oran_adapt.core.tracing import trace_id_hex
trace_id_hex("<event_id>")
```

The job row also stores the intake span's W3C `traceparent` (`adaptation_job.trace_context`).

**The span tree.**

```
intake (SERVER, API or CDC)             event_id, model_id, actor
└─ job.publish (PRODUCER)               broker message carries the traceparent
job.attempt (CONSUMER, worker)          continued from the job row; job_id, attempt
└─ job.attempt.run                      in the worker or its attempt process
   ├─ stage DATA_PREPARING ... stage PROMOTING (one span per pipeline stage)
   │  └─ <port>.<operation>             every adapter call (registry, dataset, deployment, llm ...)
   ├─ deployment.rollout / deployment.split
   │  └─ deployment.verify              the serving read-back
rollout.tick                            each canary tick continues the job's trace
```

A job recorded before the trace column existed has no stored traceparent; its spans start a
root in the event's keyed trace instead, so they still share one trace id.

**Export.** `TRACING_EXPORTER` picks the exporter; the SDK is configured at the composition
root of each process (API, worker, CLI), never at import time.

| Key | Default | Meaning |
|---|---|---|
| `TRACING_EXPORTER` | `none` | `none` (no-op spans), `console` (stderr), `jsonl` (one span per line to `TRACING_JSONL_PATH`), `otlp` (OTLP/HTTP) |
| `TRACING_SERVICE_NAME` | `oran-adapt` | the `service.name` resource attribute |
| `TRACING_SAMPLE_RATIO` | `1.0` | parent-based ratio sampler; the keyed trace id makes the decision the same for every span of an event |
| `TRACING_JSONL_PATH` | - | required with `jsonl` |
| `TRACING_OTLP_ENDPOINT` | - | required with `otlp`, e.g. `http://otel-collector:4318/v1/traces` |
| `TRACING_OTLP_TIMEOUT_S` | `10.0` | export timeout |

`otlp` needs the `otlp` extra (`pip install oran-adapt[otlp]`,
opentelemetry-exporter-otlp-proto-http); without it the setting is rejected at startup with a
configuration error rather than dropping spans silently.

## Logs

JSON lines on stderr (`LOG_JSON`, `LOG_LEVEL`). Each line carries `correlation_id` and, inside
a span, `trace_id` and `span_id`, so a line leads to its trace. Messages and error texts are
redacted before they are written: credentials in URLs (`scheme://user:secret@`),
`password=` / `token=` / `secret=` / `api_key=`-style assignments and `Bearer` tokens become
`***`. The plain-text format (`LOG_JSON=false`) is redacted the same way.

## Dashboards, alerts and runbooks

- **Dashboards:** `deploy/helm/oran-adapt/files/dashboards/*.json` (Grafana, schema 39, a
  `datasource` variable). The chart ships them as a ConfigMap with the Grafana sidecar label
  when `metrics.dashboards.enabled` is true; otherwise import the JSON files.
- **Alert rules:** `deploy/helm/oran-adapt/files/prometheus-rules.yaml`, a plain Prometheus
  rule file, rendered into a PrometheusRule when `metrics.prometheusRule.enabled` is true.
- **Runbooks:** `docs/runbooks/<Alert>.md`, one per alert, each with *Fires when*, *Impact*,
  *Check* and *Fix*.

Tests (`tests/unit/test_phase12_observability.py`) check that every expression in the rules
and the dashboards reads only exported metrics and labels, that every alert names an existing
runbook and every runbook an existing alert, and that the dashboards are well formed.
