# Integrating with the OAI RAN KPI database (`rankpi`)

This guide connects the framework to the `rankpi` database (PostgreSQL/TimescaleDB, built from
`schema.sql`, `migrations/002_kpm.sql` and `schema_model.sql`; see the "OAI RAN KPI + ML
Database Schema Reference"). It closes the integration feedback that was still open: **load
data by reference from a database query**, and read **canary/shadow health** from the team's
own ML lifecycle store. Section 6 lists where each of the ten feedback items is handled.

Example configuration: [`config/examples/rankpi.toml`](../../config/examples/rankpi.toml).
Delivery policy: [`config/policies/delivery-rankpi.toml`](../../config/policies/delivery-rankpi.toml).

## 1. The recommended flow

1. **Drift arrives** at `POST /api/v1/adaptation/events` (or a mapper,
   `POST /api/v1/adaptation/events/from/{name}`). The framework adapts. It does not detect
   drift.
2. **Data is registered by reference**, not posted as JSON rows. The historical and drifted
   versions are queries of the rate views, e.g. `rankpi:///v_slice_kpi?run_id=4` and
   `rankpi:///v_slice_kpi?run_id=5`.
3. **The candidate runs in shadow** (`delivery_strategy = "shadow"`). It is registered in MLflow
   and never moves `live` on its own. The testbed runs it as a `model_deployment` with
   `mode = 'shadow'`, next to the stable version.
4. **Health is read from `rankpi`** (`ROLLOUT_METRICS_BACKEND=rankpi`): decision scores, the
   individual reward terms, clipping and delivery failures of each arm.
5. **An operator approves**, or the rollout rolls back on a breached health rule. Compare the
   candidate with what the team would have done using `v_model_compare`.

## 2. Data by query: the `rankpi` and `sql` dataset adapters

```bash
DATASET_BACKENDS=rankpi
DATASET_RANKPI_URL=postgresql+psycopg://reader:<password>@127.0.0.1:5433/rankpi   # secret
```

Register a version by reference, through the API or the CLI, with `format = "csv"` (or end
the relation with `.csv`) and `timestamp_column = "ts"`:

```text
rankpi:///v_slice_kpi?run_id=5
rankpi:///v_slice_kpi.csv?run_id=5&sst=1&columns=ts,sst,sd,dl_thp_kbps,prb_share
rankpi:///v_ue_rate?run_id=5&run_id=6                       # repeat a filter for IN
rankpi:///v_kpm_ue?from=2026-09-29T12:00:00Z&to=2026-09-29T12:10:00Z
```

A reference names **one relation and its filters, never SQL**:

- **Allow-listed relations only.** The default `DATASET_RANKPI_RELATIONS` holds the derived
  views: `v_ue_rate`, `v_slice_rate`, `v_slice_kpi`, `v_slice_prb_share`, `v_cell_rate`,
  `v_ue_kpm`, `v_kpm_ue` and `v_kpm_slice`. The dimension tables (`ue` holds IMSIs and IPs) and
  the identity join `v_mac_ue_ident` stay unreadable unless you add them.
- **Every other parameter is an equality filter** on a column. Columns are checked against the
  relation's reflected columns, and values are bound parameters. `from` and `to` bound the time
  column (`time_column=`, default `DATASET_RANKPI_TIME_COLUMN=ts`). `columns=` picks the
  columns, which is how you leave out identifiers that are not features.
- **The query must be bounded** by `run_id` or `to`. A version must stop changing once it is
  registered.
- **The run must be closed and complete.** A `run_id` with `run.ended_at IS NULL` is refused
  (`DATASET_RANKPI_REQUIRE_CLOSED_RUN`). A run whose `v_run_summary.dropped` is above
  `DATASET_RANKPI_MAX_DROPPED` (default 0, "a nonzero value means real data loss") is also
  refused.
- **Cumulative counters and dead fields are refused as columns**: `dl_aggr_tbs`, `tx_pdu_byte`,
  `dl_pdcp_vol_kb`, `dl_prb_tot` and the rest; `slot`, `dl_mcs2`/`ul_mcs2`, `*_aggr_bytes_sdus`.
  A cumulative counter grows all run, so KS/PSI flags it as drifted every time, and the schema
  says to use MAX−MIN, never SUM. Read the rate views instead, or set
  `DATASET_RANKPI_ALLOW_CUMULATIVE=true` deliberately.
- **Read-only and time-boxed.** On PostgreSQL every query runs in a read-only transaction under
  `DATASET_SQL_STATEMENT_TIMEOUT_S`. Give the framework a role with `SELECT` on the views only.
- **The rows are fingerprinted.** The result is streamed as CSV, ordered by the time column and
  then every other column, and `stat` hashes it. If the rows behind a registered version
  change, reading it fails rather than training on other data. That happens, for example, when
  `identify_ues.py` fills `ue_session.ue_id` after registration. Register a run once
  identification has finished.

Results go through the same ceilings as any reference: `DATASET_MAX_SOURCE_BYTES`,
`DATASET_MAX_ROWS` and batches of `DATASET_CHUNK_ROWS` rows.

**Any other database** uses the generic `sql` adapter:
`DATASET_SQL_SOURCES={"tsdb": "postgresql+psycopg://..."}` (secret),
`DATASET_SQL_RELATIONS=["tsdb.kpi_*"]`, and URIs of the form
`sql://tsdb/kpi_cell_hourly?cell_id=12&to=2026-09-30T00:00:00Z`. It has the same allow-list,
binding, read-only and fingerprint rules, without the rankpi-specific checks.

## 3. Rollout health: the `rankpi` rollout metrics adapter

```bash
ROLLOUT_METRICS_BACKEND=rankpi
ROLLOUT_RANKPI_URL=postgresql+psycopg://reader:<password>@127.0.0.1:5433/rankpi   # secret
ROLLOUT_RANKPI_MODEL_NAMES={"slice-prb-policy": "ppo_slice_v3"}    # only when names differ
```

Each arm (model, version, window) maps to the `model_deployment` rows of that `ran_model.name`
and `ran_model_version.version` that overlap the window. The adapter reports these samples:

| Metric | Source | One sample per |
|---|---|---|
| `score` | `model_decision.score` | decision (the decision count is the arm's request count) |
| `inference_ms` | `model_decision.inference_ms` | decision |
| `score_term.<name>` | `score_term.contribution` | decision and term |
| `clipped` | `control_action.clipped` as 1/0 | action (mean = clipped fraction) |
| `delivery_failed` | `control_delivery.outcome` nack/timeout/error = 1, sent/ack = 0 | delivery (suppressed are skipped) |
| any `model_metric.name` | `model_metric.value` | metric row |

`config/policies/delivery-rankpi.toml` watches `score`, `clipped`, `delivery_failed` and
`inference_ms`. It also watches the **starvation and fairness terms on their own**. The schema
reference records that the `throughput` term is unbounded while those penalties are capped, so
a policy that starves a slice can out-score a fair one. A health rule on the total score alone
would promote it. Confirm each term's sign in the reward code before relying on those rules.

## 4. What the schema's open items mean here

| Open item (schema reference) | Effect on adaptation |
|---|---|
| `control_action.applied_value` is always NULL | There is no `apply_gap` metric. Health rules can only use what was achieved (`v_slice_prb_share`, `v_control_outcome.achieve_gap`), so publish it as a `model_metric` if you want it gated. |
| `dedicated_prb_ratio` is rescaled (`pct/100 * (1 - default_slice_pct)`) | A model's requested share is not the share it gets. Train and validate on what was achieved, not on the request. |
| `rnti IS NULL` in `e2sm_kpm_ue` / `kpm_ue` | KPM rows cannot be joined to MAC rows per UE. Use `v_kpm_ue`/`v_kpm_slice` or the MAC-side `v_ue_kpm`, not a join of the two. |
| Reward weighting (unbounded throughput term) | Covered by the per-term health rules above. Re-weighting the reward is a model change outside this framework. |
| `capture_sst_sd()` NULL-deref | Collector bug, outside this framework. |

## 5. Assumptions to confirm on site

These come from the schema reference. They are not taken from a live query, so check them once
against the database:

- `run(run_id, ended_at)` and `v_run_summary(run_id, dropped)` exist with those column names.
  The run checks read them.
- The views you allow-list have a `ts` column. If not, pass `time_column=` per reference or
  change `DATASET_RANKPI_TIME_COLUMN`.
- `model_deployment`, `ran_model_version`, `ran_model`, `model_decision`, `score_term`,
  `control_action`, `control_delivery` and `model_metric` have the documented columns. The
  rollout adapter reads them.

The tests (`tests/unit/test_rankpi_integration.py`) run both adapters through their port's
conformance suite against SQLite stand-ins with those columns. They have not been run against
the live `rankpi` database.

## 6. The ten integration findings and where each is handled

| # | Finding | Where it is handled | State |
|---|---|---|---|
| 1 | MLflow is the only registry | `RegistryPort`: `mlflow`, `filesystem`, `mirror`, `sagemaker`, `vertex` (Phase 2) | emulators only for cloud |
| 2 | "Deploy" only moves the alias | `DeploymentPort` with read-back: KServe, Seldon, k8s, Triton, BentoML, SageMaker, Vertex, GitOps, webhook (Phase 3) | emulators only |
| 3 | No outbound notifications | Durable outbox + webhook, Slack, PagerDuty, email, Kafka, SQS, SNS, Pub/Sub, NATS (Phase 4) | local receivers |
| 4 | Data only as JSON rows / one CDC table | `DatasetPort`: file, http, fsspec, s3, gcs (Phase 5); **`sql` and `rankpi` by query (this change)**; CDC from any table | sql/rankpi: SQLite only |
| 5 | Jobs are threads in the API | Durable queue, leases, deadlines, cancel; `database`, `celery`, `rq`, `kubernetes` (Phase 6) | brokers: doubles |
| 6 | All traffic switches at once; lenient gate | Superiority gate with guardrails (Phase 7); shadow, canary, A/B, manual delivery; **rankpi health source (this change)** | in-process |
| 7 | Images, compose, Helm never built | Dockerfile targets, compose, Helm chart, kustomize, CI jobs (Phase 11) | CI only, not run locally |
| 8 | LLM needs internet | Off by default; rules-only path; self-hosted provider option (Phase 10) | live providers unverified |
| 9 | API keys only | OIDC, gateway assertions, mTLS, OPA, Vault (Phase 9) | doubles only |
| 10 | Tabular sklearn/XGBoost/FF-torch only | Model-type plugins incl. RNN/LSTM/GRU/TCN/transformer and statsmodels (Phase 8) | some libs via doubles |

The "State" column follows [LIMITATIONS.md](../LIMITATIONS.md). Anything not marked as run
against the real system has not been run against it.
