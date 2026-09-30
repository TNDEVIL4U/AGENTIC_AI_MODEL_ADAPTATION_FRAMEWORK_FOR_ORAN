# Validation gate, progressive delivery and rollout metrics adapters

A retrained candidate now reaches traffic in two steps:

1. **The gate** (`validation/gate.py`, policy `GATE_POLICY`) decides offline, on held-out data,
   whether the candidate may replace the incumbent. Every decision is stored (`gate_decision`
   table, `GET /api/v1/models/{model_id}/gate-decisions`, audit action `GATE_DECIDED`) with the
   policy's `version` and `policy_hash`.
2. **Delivery** (`delivery/controller.py`, `DELIVERY_STRATEGY` and `DELIVERY_POLICY`) moves an
   accepted candidate onto traffic: all at once (`blue_green`) or as a rollout that watches
   online health and promotes or rolls back on evidence.

A **rollout metrics adapter** implements `oran_adapt.ports.RolloutMetricsPort`: it tells the
controller what each arm (`stable`, `candidate`) of a rollout showed online over a time window.
`ROLLOUT_METRICS_BACKEND` selects it; the core finds adapters through the
`oran_adapt.rollout_metrics` entry-point group (`bootstrap.build_rollout_metrics`).

| Adapter | Module | Metrics come from | Needs | Verified |
|---|---|---|---|---|
| `api` (default) | `adapters/rollout_metrics.py` | `POST /api/v1/rollouts/{id}/observations` (stored as `rollout_observation` rows) | - | local |
| `prometheus` | `adapters/rollout_metrics.py` | PromQL range queries per metric over the arm's window | `ROLLOUT_PROMETHEUS_URL`, `ROLLOUT_PROMETHEUS_QUERIES` | mock transport, unverified against Prometheus |

## The gate

The primary metric (accuracy / F1 / RMSE / MAE / silhouette ..., per task type) is compared with
a **paired bootstrap** over the same held-out rows: each resample draws row indices once and
scores both models on them, so the interval is of the *improvement*, oriented so positive is
better. With `confidence` 0.95 the two-sided interval is `[ci_low, ci_high]`:

| `mode` | Accepts when | Meaning |
|---|---|---|
| `superiority` (default) | `ci_low > margin` | the candidate is better by more than `margin`; an interval touching zero is rejected, so a marginally-worse or equal candidate never ships |
| `non_inferiority` | `ci_low >= -margin` | the candidate is at most `margin` worse |

`margin_relative = true` makes the margin a fraction of the incumbent's value (for error
metrics with no fixed scale). **Guardrails** reject on their own, whatever the primary metric
says: per-slice regression (`slices.columns`, `max_drop`, `min_rows`), calibration (ECE,
binary classifiers with probabilities), prediction latency, serialized size, and
`metric_guards` (secondary metric -> largest allowed drop). A first version (no incumbent) is
judged on its own and accepted when it can be scored.

The decision (`ValidationReport.gate`) lists the verdict, delta, interval, threshold, every
guardrail with its measured values, and the reasons. Resamples are seeded (`seed`), so the same
data and policy give the same decision. Cost is bounded by `max_rows`, `resamples` and `batch`.

## Delivery strategies

| `DELIVERY_STRATEGY` | What happens after the gate accepts | Needs `traffic_split` |
|---|---|---|
| `blue_green` | `promote_version` moves LIVE at once, with read-back; the job ends `REGISTERED` | no |
| `shadow` (default) | rollout `SHADOW`: the candidate serves no traffic, mirrored-traffic metrics are compared; once healthy, `shadow_then` (`manual`, `canary` or `promote`); `EXPIRED` after `shadow_max_s` | only if it continues as a canary |
| `canary` | rollout `CANARY` through `canary_steps` (e.g. 5, 25, 50, 100 %), each held `canary_step_hold_s` while healthy; the last step promotes | yes |
| `ab` | rollout `AB` at `ab_percent` for `ab_duration_s`, then a Welch t-test on `ab_metric` at `ab_confidence`: better promotes, worse rolls back, inconclusive follows `ab_inconclusive` | yes |
| `manual` | rollout `AWAITING_APPROVAL`: `POST /rollouts/{id}/approve` within `approval_ttl_s` runs `approval_then` (`promote` or `canary`); `reject` ends it; unanswered it becomes `EXPIRED` | only for `approval_then = "canary"` |

A job that starts a rollout ends `COMPLETED` with outcome `DELIVERING`; LIVE moves only when the
rollout promotes. While a model has an active rollout, new events for it finish with outcome
`ROLLOUT_IN_PROGRESS` and start no adaptation. Settings validation (startup and
`oran-adapt config lint`) refuses a strategy that needs a traffic split with a deployment
backend that has none (docs/adapters/deployment.md).

### Health and the controller

Every `ROLLOUT_TICK_S` a worker (or `oran-adapt rollout tick`) advances each active rollout
under the model's lock (holder `rollout:<id>`), so a tick and an adaptation job never act on
one model together. For each arm the adapter's `observe` returns the samples since the current
step began. `DELIVERY_POLICY.health` rules then judge the candidate against stable:

* nothing is judged before both arms have `min_samples` requests (`insufficient`: wait);
* a rule breaches when the candidate's mean is beyond `limit`, more than `max_degradation`
  worse than stable's, or worse by more than the ratio `max_ratio` (`direction` says which
  way is better); a `required` metric without samples keeps the verdict `insufficient`;
* any breach rolls back: the split is removed and **read back** from the serving system, and
  the rollout ends `ROLLED_BACK`. LIVE never moved, so nothing else needs undoing.

Promotion goes through `promote_version` with the idempotency key `rollout:<id>:promote` and
`expected_live = stable`, so a retried tick cannot promote twice and a LIVE moved by someone
else fails the promotion (then the rollout rolls back). If removing a split fails, or the
metrics source is unreachable (`RolloutMetricsUnavailableError`), the rollout stays where it is
with a note in its history and the next tick decides again from fresh evidence.

Every transition is appended to the rollout's `history` and recorded as an audit entry
(`ROLLOUT_STARTED`, `ROLLOUT_ADVANCED`, `ROLLOUT_FINISHED`), a notification event
`rollout.<state>` and metrics (`rollouts_started_total{strategy}`,
`rollouts_finished_total{strategy,state}`, `rollouts_active{state}`, `rollback_total{trigger="rollout"}`,
`gate_decisions_total{verdict,mode}`, `traffic_splits_total{backend,outcome}`). The rollout keeps the
`DeliveryPolicy` (and its hash) it started under: changing the policy later does not alter a
running rollout.

## API and CLI

```
GET  /api/v1/rollouts?model_id=&state=&active=&limit=&offset=     read
GET  /api/v1/rollouts/{id}                                        read
POST /api/v1/rollouts/{id}/approve   {"reason": "..."}            promote (409 unless AWAITING_APPROVAL and not expired)
POST /api/v1/rollouts/{id}/reject    {"reason": "..."}            promote (409 once ended)
POST /api/v1/rollouts/{id}/observations                            submit (201)
     {"arm": "candidate", "requests": 50, "metrics": {"error_rate": 0.01, "latency_p95_ms": 42}}
GET  /api/v1/models/{model_id}/gate-decisions                     read

oran-adapt rollout tick [--rollout-id ID]
oran-adapt rollout list [--model-id M] [--state S] [--active] [--limit N]
oran-adapt rollout approve --rollout-id ID [--reason TEXT]
oran-adapt rollout reject  --rollout-id ID [--reason TEXT]
```

The serving layer (or a sidecar) posts observations for the `api` adapter; each observation
covers `requests` requests and carries up to 50 finite metric values.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `GATE_POLICY` / `GATE_POLICY_FILE` | superiority, margin 0, 95 %, 1000 resamples, all guardrails on | the gate's versioned policy (TOML/JSON; `config/policies/gate.toml`) |
| `DELIVERY_STRATEGY` | `shadow` | `shadow`, `canary`, `blue_green`, `ab`, `manual` |
| `DELIVERY_POLICY` / `DELIVERY_POLICY_FILE` | see `core/policies.py` | thresholds and timings (`config/policies/delivery.toml`) |
| `ROLLOUT_METRICS_BACKEND` | `api` | `api` or `prometheus` |
| `ROLLOUT_TICK_S` | 30 | how often a worker advances rollouts |
| `ROLLOUT_PROMETHEUS_URL`, `_TOKEN` | - | Prometheus base URL and bearer token |
| `ROLLOUT_PROMETHEUS_QUERIES` | `{}` | metric name -> PromQL; `{model}`, `{version}`, `{arm}`, `{rollout_id}`, `{window_s}` are filled in |
| `ROLLOUT_PROMETHEUS_REQUESTS_QUERY` | - | instant query counting an arm's requests; without it, the longest sample list counts |
| `ROLLOUT_PROMETHEUS_STEP_S`, `_TIMEOUT_S` | 60, 10 | range-query step and HTTP timeout |
| `DEPLOYMENT_CANARY_ALIAS`, `DEPLOYMENT_TRAFFIC_TAG` | `canary`, `oran.traffic_percent` | how `registry-alias` expresses a split |

Example Prometheus queries:

```toml
ROLLOUT_PROMETHEUS_QUERIES = '{"error_rate": "sum(rate(http_requests_total{model=\"{model}\",version=\"{version}\",code=~\"5..\"}[1m])) / sum(rate(http_requests_total{model=\"{model}\",version=\"{version}\"}[1m]))", "latency_p95_ms": "1000 * histogram_quantile(0.95, sum by (le) (rate(request_seconds_bucket{model=\"{model}\",version=\"{version}\"}[1m])))"}'
ROLLOUT_PROMETHEUS_REQUESTS_QUERY = 'sum(increase(http_requests_total{model="{model}",version="{version}"}[{window_s}s]))'
```

## Writing a new adapter

Start from `templates/rollout-metrics-adapter/`. An adapter is a class with

* `ping()`: raise `RolloutMetricsUnavailableError` when the source cannot be reached;
* `observe(session, window) -> ArmStats`: every sample of each metric for `window.arm` (serving
  `window.version` of `window.model` in rollout `window.rollout_id`) observed between
  `window.start` and `window.end`, as finite floats, plus how many requests they cover. Raise
  `RolloutMetricsUnavailableError` (never another class) when the source is down: the
  controller then waits instead of deciding on missing evidence.

Register an `AdapterSpec` under the `oran_adapt.rollout_metrics` entry-point group (vendor SDKs
imported lazily inside the adapter) and run the conformance suite
(`oran_adapt.conformance.rollout_metrics`):

| Check | Rule |
|---|---|
| `protocol` | implements the port; `ping` succeeds |
| `empty_window` | a window with no observations reads count 0 and no samples |
| `reads_back` | seeded observations come back with their values |
| `arms_separate` | one arm's samples are never read as the other's |
| `window_bounds` | observations outside `[start, end]` are not read |
| `pickle` | survives pickling (workers run in other processes) |
| `unreachable_source` | an unreachable source raises `RolloutMetricsUnavailableError` from `ping` and `observe` |

```python
from oran_adapt.conformance.rollout_metrics import CHECKS, Context

@pytest.mark.parametrize("check", sorted(CHECKS))
def test_my_source(check, session):
    CHECKS[check](MySource(...), Context(session=session, seed=my_seed, break_source=my_break))
```
