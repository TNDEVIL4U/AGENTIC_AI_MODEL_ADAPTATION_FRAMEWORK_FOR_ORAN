"""Prometheus metrics, exposed at ``GET /api/v1/metrics``.

Metrics are registered once, at import, on prometheus_client's default registry. Label values
are always from small closed sets (outcomes, strategies, route templates), never ids, so the
number of series stays bounded."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

# Jobs take seconds to many minutes; model scoring milliseconds to a minute.
_JOB_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
_EVAL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

ADAPTATION_JOBS = Counter(
    "adaptation_jobs_total",
    "Adaptation jobs that finished, by final status and outcome.",
    ["status", "outcome"],
)
ADAPTATION_SUCCESS = Counter(
    "adaptation_success_total",
    "Adaptation jobs that completed (any outcome other than a failure or a rolled-back promotion).",
    ["outcome"],
)
ADAPTATION_FAILURE = Counter(
    "adaptation_failure_total",
    "Adaptation jobs that failed or whose promotion was rolled back, by error code.",
    ["reason"],
)
ADAPTATION_IN_PROGRESS = Gauge("adaptation_jobs_in_progress", "Adaptation jobs running now.")
ADAPTATION_DURATION = Histogram(
    "adaptation_duration_seconds",
    "Wall-clock time of one adaptation job, from acceptance to its final state.",
    buckets=_JOB_BUCKETS,
)
MODEL_REUSE = Counter(
    "model_reuse_total", "Existing model versions promoted to LIVE instead of training."
)
FINE_TUNE = Counter("fine_tune_total", "Fine-tuning runs started.")
RETRAIN = Counter("retrain_total", "Full retraining runs started.")
ROLLBACK = Counter(
    "rollback_total",
    "Moves of LIVE back to an earlier version: manual rollbacks, and promotions undone after a "
    "failure.",
    ["trigger"],
)
VALIDATION_FAILURE = Counter(
    "validation_failure_total", "Candidates rejected by the validation gate."
)
MODEL_EVALUATION_DURATION = Histogram(
    "model_evaluation_duration_seconds",
    "Time to score every registered version of a model on the current data.",
    buckets=_EVAL_BUCKETS,
)
CDC_EVENTS = Counter(
    "cdc_events_total", "Change-data-capture events processed.", ["source", "operation"]
)
CDC_PROCESSING_LAG = Gauge(
    "cdc_processing_lag",
    "Seconds between a source row change and the CDC consumer processing it (latest event).",
)
LLM_REQUESTS = Counter("llm_requests_total", "Calls made to the LLM provider.", ["provider"])
LLM_FAILURES = Counter("llm_failures_total", "LLM calls that failed.", ["provider"])
SANDBOX_FAILURES = Counter(
    "sandbox_failures_total",
    "Generated adapters refused by the code scan or failing inside the sandbox.",
    ["reason"],
)
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests served.", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency.", ["method", "route"]
)


def render() -> tuple[bytes, str]:
    """The current metrics in the Prometheus text format, and its content type."""
    return _generate_latest(REGISTRY), CONTENT_TYPE_LATEST
