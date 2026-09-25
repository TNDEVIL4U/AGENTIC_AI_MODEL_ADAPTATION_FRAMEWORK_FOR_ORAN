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
DRIFT_EVENTS = Counter(
    "drift_events_total",
    "Drift events analysed, by the analysis outcome (REUSE, PACKAGED, INSUFFICIENT_DATA).",
    ["outcome"],
)
STRATEGY_SELECTED = Counter(
    "strategy_selected_total",
    "Adaptation strategies chosen, by strategy and by what chose it (constraint, LLM, fallback).",
    ["strategy", "source"],
)
REGISTRATIONS = Counter("model_registrations_total", "Candidate model versions registered.")
PROMOTIONS = Counter(
    "model_promotions_total", "Live-alias moves applied, by kind (promotion, reuse, rollback).", ["kind"]
)
JOB_TIMEOUTS = Counter(
    "job_timeouts_total",
    "Adaptation jobs recorded TIMED_OUT, by the stage their worker had reached when killed.",
    ["stage"],
)

# The metrics a job's worker process can move. A worker runs in its own process with its own
# registry, so it reports how far each of these moved (delta_since) and the parent, which
# serves /metrics, applies that (apply_delta). A worker killed on a timeout reports nothing.
_FORWARDED = (
    DRIFT_EVENTS,
    STRATEGY_SELECTED,
    REGISTRATIONS,
    PROMOTIONS,
    MODEL_REUSE,
    FINE_TUNE,
    RETRAIN,
    ROLLBACK,
    VALIDATION_FAILURE,
    MODEL_EVALUATION_DURATION,
    CDC_EVENTS,
    LLM_REQUESTS,
    LLM_FAILURES,
    SANDBOX_FAILURES,
)
_BY_NAME = {family.name: metric for metric in _FORWARDED for family in metric.describe()}

MetricDelta = list[tuple[str, str, dict[str, str], float]]


def _values() -> dict[tuple[str, str, tuple], float]:
    out: dict[tuple[str, str, tuple], float] = {}
    for metric in _FORWARDED:
        for family in metric.collect():
            for s in family.samples:
                if s.name.endswith(("_total", "_bucket", "_sum")):
                    out[(family.name, s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def snapshot() -> dict[tuple[str, str, tuple], float]:
    """The forwarded metrics' sample values now, to diff against later with delta_since."""
    return _values()


def delta_since(before: dict[tuple[str, str, tuple], float]) -> MetricDelta:
    """How far each forwarded sample moved since ``before``, as plain picklable tuples."""
    return [
        (family, sample, dict(labels), value - before.get((family, sample, labels), 0.0))
        for (family, sample, labels), value in _values().items()
        if value != before.get((family, sample, labels), 0.0)
    ]


def apply_delta(delta: MetricDelta) -> None:
    """Add a worker's delta_since result to this process's metrics. Counters get the same
    increments; histograms get the same per-bucket counts and sum, so nothing is estimated."""
    histograms: dict[tuple[str, tuple], dict] = {}
    for family, sample, labels, amount in delta:
        metric = _BY_NAME.get(family)
        if metric is None or amount <= 0:
            continue
        if sample.endswith("_total"):
            if isinstance(metric, Counter):
                (metric.labels(**labels) if labels else metric).inc(amount)
            continue
        plain = tuple(sorted((k, v) for k, v in labels.items() if k != "le"))
        entry = histograms.setdefault((family, plain), {"buckets": {}, "sum": 0.0})
        if sample.endswith("_bucket"):
            entry["buckets"][float(labels["le"])] = amount
        else:
            entry["sum"] = amount
    for (family, plain), entry in histograms.items():
        metric = _BY_NAME[family]
        if not isinstance(metric, Histogram):
            continue
        child = metric.labels(**dict(plain)) if plain else metric
        # Bucket samples are cumulative; the child keeps one counter per bucket. These are
        # prometheus_client internals (stable since 0.4) - there is no public "add counts" API.
        cumulative = 0.0
        for i, bound in enumerate(child._upper_bounds):
            count = entry["buckets"].get(bound, cumulative)
            if count > cumulative:
                child._buckets[i].inc(count - cumulative)
            cumulative = max(cumulative, count)
        child._sum.inc(entry["sum"])


def render() -> tuple[bytes, str]:
    """The current metrics in the Prometheus text format, and its content type."""
    return _generate_latest(REGISTRY), CONTENT_TYPE_LATEST
