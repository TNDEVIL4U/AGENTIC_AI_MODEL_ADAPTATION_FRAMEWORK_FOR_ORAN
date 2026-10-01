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
# Fallback reasons are a closed set (llm.calls.FALLBACK_* and LlmUnavailableError.reason).
LLM_FALLBACKS = Counter(
    "llm_fallbacks_total",
    "Decisions and adaptations the deterministic rules made because the LLM path failed.",
    ["reason"],
)
LLM_TOKENS = Counter(
    "llm_tokens_total", "Tokens sent to and received from the LLM.", ["provider", "direction"]
)
LLM_COST = Counter("llm_cost_total", "LLM spend in the LLM_COST_* currency.", ["provider"])
LLM_REFUSED = Counter(
    "llm_calls_refused_total",
    "LLM calls refused before sending (circuit open, over a cap).",
    ["provider", "reason"],
)
LLM_CIRCUIT_OPEN = Gauge(
    "llm_circuit_open", "1 while the LLM circuit breaker is open (this process).", ["provider"]
)
# Notification sink labels come from NOTIFICATION_BACKEND, a short configured list.
NOTIFICATION_EVENTS = Counter(
    "notification_events_total", "Events written to the notification outbox.", ["event_type"]
)
NOTIFICATION_DELIVERIES = Counter(
    "notification_deliveries_total",
    "Notification delivery attempts by sink and outcome (delivered, retry, dead).",
    ["sink", "outcome"],
)
NOTIFICATION_DELIVERY_DURATION = Histogram(
    "notification_delivery_duration_seconds", "Time a sink took to take a message.", ["sink"],
    buckets=_EVAL_BUCKETS,
)
NOTIFICATION_BACKLOG = Gauge(
    "notification_backlog", "Deliveries waiting (PENDING) or dead-lettered (DEAD).", ["status"]
)
NOTIFICATION_CIRCUIT_OPEN = Gauge(
    "notification_circuit_open", "1 while a sink's circuit breaker is open.", ["sink"]
)
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
DEPLOYMENTS = Counter(
    "model_deployments_total",
    "Rollouts to the serving system (DEPLOYMENT_BACKEND), by outcome: ok (read back as serving) "
    "or failed (previous version restored).",
    ["backend", "outcome"],
)
TRAFFIC_SPLITS = Counter(
    "traffic_splits_total",
    "Traffic split changes (canary and A/B rollouts), by outcome: ok (read back) or failed.",
    ["backend", "outcome"],
)
GATE_DECISIONS = Counter(
    "gate_decisions_total",
    "Validation gate verdicts, by verdict (ACCEPT, REJECT) and gate mode.",
    ["verdict", "mode"],
)
ROLLOUTS_STARTED = Counter(
    "rollouts_started_total", "Progressive rollouts started, by strategy.", ["strategy"]
)
ROLLOUTS_FINISHED = Counter(
    "rollouts_finished_total",
    "Progressive rollouts ended, by strategy and final state (PROMOTED, ROLLED_BACK, "
    "EXPIRED, REJECTED).",
    ["strategy", "state"],
)
ROLLOUTS_ACTIVE = Gauge(
    "rollouts_active", "Rollouts not yet ended, by state (set on every tick).", ["state"]
)
JOB_TIMEOUTS = Counter(
    "job_timeouts_total",
    "Adaptation jobs recorded TIMED_OUT, by the stage their worker had reached when killed.",
    ["stage"],
)
JOB_QUEUE_DEPTH = Gauge(
    "job_queue_depth",
    "Jobs QUEUED and not quarantined, by worker class (set by the reaper).",
    ["worker_class"],
)
JOB_QUEUE_OLDEST_AGE = Gauge(
    "job_queue_oldest_age_seconds",
    "Age of the oldest QUEUED job, by worker class (set by the reaper).",
    ["worker_class"],
)
JOB_REQUEUES = Counter(
    "job_requeues_total",
    "Jobs put back in the queue, by reason: retry (transient error), lost (worker died), "
    "lease_expired (found by the reaper) or drained (worker shut down).",
    ["reason"],
)
JOB_QUARANTINED = Counter(
    "job_quarantined_total",
    "Poison jobs quarantined after JOB_POISON_THRESHOLD attempts without an outcome.",
)
JOB_CANCELLED = Counter(
    "job_cancelled_total",
    "Jobs recorded CANCELLED, by where the cancel found them: queued or running.",
    ["where"],
)
DATASET_ROWS_READ = Counter(
    "dataset_rows_read_total",
    "Data rows read for analysis and training, by how the version stores them "
    "(rows, reference, derived).",
    ["storage"],
)
DATASET_BYTES_READ = Counter(
    "dataset_bytes_read_total",
    "Bytes read from referenced data objects, by URI scheme.",
    ["scheme"],
)
DATASET_READ_DURATION = Histogram(
    "dataset_read_duration_seconds",
    "Time to read one data version's rows, by how the version stores them.",
    ["storage"],
    buckets=_EVAL_BUCKETS,
)
DATASET_READ_REFUSED = Counter(
    "dataset_read_refused_total",
    "Data reads refused, by reason: too_large (over DATASET_MAX_ROWS or "
    "DATASET_MAX_SOURCE_BYTES), changed (the referenced object no longer matches) or "
    "not_allowed (URI outside the configured allow-lists).",
    ["reason"],
)
STAGE_DURATION = Histogram(
    "adaptation_stage_duration_seconds",
    "Time a job attempt spent in each pipeline stage, by stage and outcome (ok, error).",
    ["stage", "outcome"],
    buckets=_JOB_BUCKETS,
)
# Adapter calls (core.observed): the port, the adapter name the configuration chose and the
# port method - three closed sets, so the series stay bounded.
_ADAPTER_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)
ADAPTER_CALL_DURATION = Histogram(
    "adapter_call_duration_seconds",
    "Latency of calls to a port's adapter, by port, adapter and operation.",
    ["port", "adapter", "operation"],
    buckets=_ADAPTER_BUCKETS,
)
ADAPTER_ERRORS = Counter(
    "adapter_errors_total",
    "Adapter calls that raised, by port, adapter, operation and error code (the AdaptationError "
    "code, else UNEXPECTED).",
    ["port", "adapter", "operation", "code"],
)
ROLLOUT_STEPS = Counter(
    "rollout_steps_total",
    "Rollout transitions that did not end the rollout (canary steps, shadow to canary, "
    "approval waits), by strategy and the state entered.",
    ["strategy", "state"],
)
DELIVERY_FAILURES = Counter(
    "delivery_failures_total",
    "Progressive delivery steps that could not complete, by strategy and reason: first_split "
    "(the first traffic change did not read back), serving (a split or undo failed), metrics "
    "(the rollout metrics source was unavailable), promotion (LIVE could not move).",
    ["strategy", "reason"],
)
WORKER_INFO = Gauge(
    "worker_up", "1 while this worker process serves its metrics port.", ["worker_class"]
)

# The metrics a job's worker process can move. A worker runs in its own process with its own
# registry, so it reports how far each of these moved (delta_since) and the parent, which
# serves /metrics, applies that (apply_delta). A worker killed on a timeout reports nothing.
_FORWARDED = (
    DRIFT_EVENTS,
    STRATEGY_SELECTED,
    REGISTRATIONS,
    PROMOTIONS,
    DEPLOYMENTS,
    TRAFFIC_SPLITS,
    GATE_DECISIONS,
    ROLLOUTS_STARTED,
    ROLLOUTS_FINISHED,
    MODEL_REUSE,
    FINE_TUNE,
    RETRAIN,
    ROLLBACK,
    VALIDATION_FAILURE,
    MODEL_EVALUATION_DURATION,
    CDC_EVENTS,
    LLM_REQUESTS,
    LLM_FAILURES,
    LLM_FALLBACKS,
    LLM_TOKENS,
    LLM_COST,
    LLM_REFUSED,
    SANDBOX_FAILURES,
    DATASET_ROWS_READ,
    DATASET_BYTES_READ,
    DATASET_READ_DURATION,
    DATASET_READ_REFUSED,
    STAGE_DURATION,
    ADAPTER_CALL_DURATION,
    ADAPTER_ERRORS,
    ROLLOUT_STEPS,
    DELIVERY_FAILURES,
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


def serve(port: int, addr: str = "") -> None:
    """Serve this process's metrics on ``port`` (a worker: WORKER_METRICS_PORT), in a daemon
    thread that ends with the process."""
    from prometheus_client import start_http_server

    start_http_server(port, addr=addr)


def render() -> tuple[bytes, str]:
    """The current metrics in the Prometheus text format, and its content type."""
    return _generate_latest(REGISTRY), CONTENT_TYPE_LATEST
