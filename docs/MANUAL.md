# Operator Manual

A complete, copy-pasteable reference for setting up, running, testing, and calling every part
of this project. Written for Windows + PowerShell/Git-Bash, since that's what this repo is
developed on; Linux/macOS equivalents are noted where the commands differ.

All commands below assume your shell's current directory is the repo root.

---

## 1. One-time setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (cmd/PowerShell)
# source .venv/bin/activate     # macOS/Linux

pip install -e ".[dev]"
```

> **Known gap in this checkout:** as of this writing, `oran_adapt` is **not** installed as an
> editable package in `.venv` (`pip show oran-adapt` reports "Package(s) not found"), even
> though `pytest` works because `pyproject.toml` sets `pythonpath = ["src"]` for the test
> runner only. Run `pip install -e ".[dev]"` to fix this properly, or — until you do — export
> `PYTHONPATH` to `src` before running any `python -m uvicorn` or ad-hoc script command (every
> command below that needs it already includes it).

Copy the example environment file and fill in real values if you need them:

```bash
cp .env.example .env
```

Every setting has a safe local default except LLM keys (only needed for the LLM-fallback path).
See §7 for the full variable reference.

---

## 2. Database migrations

Run once after setup, and again any time `migrations/versions/` gains a new revision:

```bash
python -c "from oran_adapt.db.migrate import upgrade_to_head; from oran_adapt.core.config import get_settings; upgrade_to_head(get_settings().database_url)"
```

This targets whatever `DATABASE_URL` resolves to (default: `sqlite:///./data/oran_adapt.db`).

**Git-Bash path pitfall:** if you ever set `DATABASE_URL`/`MLFLOW_TRACKING_URI` yourself using
`$(pwd)` inside Git-Bash, you will get a POSIX-style path like `/c/AGENTIC AI .../data/app.db`.
SQLAlchemy/MLflow (running under the Windows Python in `.venv`) do **not** understand `/c/...`
paths — they get treated as a literal relative folder name, silently creating a bogus `C:\c\...`
tree instead of writing where you meant. **Always use `pwd -W`** (Git-Bash's built-in
Windows-path variant) when building a `sqlite:///` URL from the current directory, e.g.:

```bash
WINPWD="$(pwd -W)"
export DATABASE_URL="sqlite:///${WINPWD}/data/mystore/app.db"
```

---

## 3. Running the API server

```bash
export PYTHONPATH="$(pwd -W)/src"      # only needed until `pip install -e .` is fixed, see §1
python -m uvicorn oran_adapt.api.main:app --host 127.0.0.1 --port 8000 --reload
```

- Interactive docs: **http://127.0.0.1:8000/docs** (Swagger UI — try requests straight from the
  browser)
- Machine-readable schema: **http://127.0.0.1:8000/openapi.json**

`--reload` restarts the server on source changes; drop it for anything resembling production.
The server needs a migrated database (§2) to start cleanly, and MLflow's SQLite store is created
automatically on first use.

To stop it: `Ctrl+C` in the terminal running it, or send `SIGTERM`/kill the process if it's
backgrounded.

---

## 4. Running the demo script

The fastest way to see the whole pipeline work end to end with **zero setup** (it migrates its
own throwaway SQLite database under `data/demo/`, seeds a model, and drives the real app via
`TestClient` — no server process, no Docker, no LLM key needed):

```bash
python scripts/demo.py
```

Expected output includes, in order: a readiness check, a no-drift event resulting in
`NO_ACTION`, a genuine-drift event that trains → validates → registers MLflow model version 2 and
moves the `live` alias to it, and a duplicate-submission check proving idempotency. See
`docs/PHASE12_DEMO_DOCS.md` for a fully verified sample run.

**Multi-model demo against a real MLflow server.** Run `python scripts/demo_models.py --start-server`.
It starts a local `mlflow server` on port 5000 (via `scripts/run_mlflow_server.py`), runs every
scenario against it, and stops the server on exit. Without `--start-server` it uses a throwaway
SQLite MLflow store instead. `--tracking-uri http://host:port` points it at a server you already
run.

The run covers:

- eight models onboarded through `onboard_model()`: sklearn Ridge, RandomForest, SGD
  `partial_fit`, a classifier, xgboost, and torch MLPs;
- a drift event for each model, where every scenario must reach its expected outcome;
- an idempotent resubmission;
- a lineage check on every registered version: the MLflow tags, the snapshot's row count and
  hash, its ancestors, and the `TRAINING` link;
- a second cycle on `cell-throughput-ridge`. It ingests `drift-2` through `POST /datasets/.../versions`
  (201, then 200 on replay, then 409 on a clash), confirms the analysis baseline is now the
  cycle-1 snapshot `train-cell-throughput-ridge-v2`, and registers v3.

---

## 5. Calling the API endpoints

All endpoints are under the `/api/v1` prefix. Examples use `curl`; swap the host/port for
wherever you started the server (§3).

### `GET /api/v1/health` — liveness

Does **not** touch the database or MLflow — just proves the process is alive.

```bash
curl http://127.0.0.1:8000/api/v1/health
```

```json
{"status": "ok", "version": "0.1.0"}
```

Always `200`.

### `GET /api/v1/ready` — readiness

Checks the database **and** MLflow are both reachable.

```bash
curl http://127.0.0.1:8000/api/v1/ready
```

```json
{
  "ready": true,
  "components": [
    {"name": "database", "ok": true, "detail": null},
    {"name": "mlflow", "ok": true, "detail": null}
  ]
}
```

`200` if both components are `ok`; `503` (with `"ready": false` and a `detail` message on the
failing component) if either is down.

### `POST /api/v1/adaptation/events` — submit a drift notification

This is the one endpoint that does real work: it analyzes drift, decides a strategy, adapts the
model, validates the candidate, and registers/promotes it in MLflow — or short-circuits to
`NO_ACTION` if there's no drift to act on.

**Request body** (`DriftEvent`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `model_id` | string | yes | Must match a model already registered via `ModelMetadata` (see §6 for how the demo/verify scripts seed one) |
| `drift_detected` | bool | yes | `false` → immediate `NO_ACTION`, no training happens |
| `event_id` | string | no | Caller-supplied idempotency key; omit it and a content hash is used instead |
| `model_type` | string | no | Free-form hint |
| `drift_score` | float 0.0–1.0 | no | |
| `evidence` | object | no | `{"feature": ..., "statistic": ..., "p_value": ...}`, extra fields allowed |
| `dataset_id` | string | no | |
| `drifted_data_version` | string | no | |
| `detected_at` | ISO 8601 datetime | no | |

**Minimal example — no drift:**

```bash
curl -X POST http://127.0.0.1:8000/api/v1/adaptation/events \
  -H "Content-Type: application/json" \
  -d '{"model_id":"my-model","event_id":"evt-1","drift_detected":false}'
```

```json
{
  "job_id": "…",
  "model_id": "my-model",
  "status": "COMPLETED",
  "strategy": null,
  "result": {"model_id": "my-model", "outcome": "NO_ACTION", "reason": "caller reported no drift", "strategy": null, "decision": null, "candidate": null, "validation": null, "registered_version": null},
  "error": null,
  "duplicate": false,
  "created_at": "…",
  "updated_at": "…"
}
```

HTTP `201` (new job) or `200` (idempotent replay of an already-processed `event_id` — same
`job_id` comes back, `duplicate: true`).

**Example — genuine drift** (needs enough drifted rows and a supported-framework model seeded
first, or the pipeline reports `NO_ACTION`/`REJECTED` for lack of data):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/adaptation/events \
  -H "Content-Type: application/json" \
  -d '{"model_id":"my-model","event_id":"evt-2","drift_detected":true}'
```

```json
{
  "job_id": "…",
  "status": "COMPLETED",
  "strategy": "FULL_RETRAINING",
  "result": {
    "outcome": "REGISTERED",
    "reason": "candidate validated (…) and registered as version 2",
    "strategy": "FULL_RETRAINING",
    "decision": {"strategy": "FULL_RETRAINING", "confidence": 0.5, "rationale": "…", "source": "FALLBACK"},
    "candidate": {"engine": "SKLEARN_FULL_RETRAIN", "framework": "sklearn", "metrics": {"accuracy": 1.0}, "…": "…"},
    "validation": {"passed": true, "current_value": 1.0, "candidate_value": 1.0, "threshold": 0.02, "…": "…"},
    "registered_version": "2"
  }
}
```

**Response fields** (`JobResponse`):

| Field | Meaning |
|---|---|
| `job_id` | Stable id for this job; resubmitting the same `event_id` returns the same one |
| `status` | One of the `JobStatus` values below — `COMPLETED` unless something failed |
| `strategy` | `FINE_TUNING`, `FULL_RETRAINING`, `ROLLBACK`, `NO_ACTION`, `INSUFFICIENT_INFORMATION`, or `NO_COMPATIBLE_STRATEGY` |
| `result` | A `JobResult`. Its `outcome` is `NO_ACTION`, `REGISTERED`, or `REJECTED`. It also carries the full decision/candidate/validation trail and, when a version is registered, `training_data_version` (the snapshot it was trained on). |
| `error` | `null` on success; otherwise `{"code": ..., "message": ..., "context": {...}}` (see error codes below) |
| `duplicate` | `true` if this `event_id` had already been processed |

**Error codes** you may see in `error.code` (each maps to an HTTP status):

| Code | HTTP status | Meaning |
|---|---|---|
| `MODEL_NOT_FOUND` | 404 | `model_id` has no `ModelMetadata` row |
| `MLFLOW_UNAVAILABLE` | 503 | MLflow tracking store unreachable |
| `DATABASE_UNAVAILABLE` | 503 | App database unreachable |
| `JOB_TIMEOUT` | 504 | Job exceeded `JOB_TIMEOUT_S` wall-clock budget |
| `VALIDATION_FAILED` | 500 | Candidate failed the accuracy/RMSE gate |
| `ADAPTATION_UNSUPPORTED` | 500 | No engine and no LLM fallback available for this framework |
| `UNSAFE_CODE_REJECTED` | 500 | LLM-generated adaptation code failed the AST security scan |
| `SANDBOX_EXECUTION_FAILED` | 500 | Sandboxed code ran but errored, timed out, or exceeded limits |
| `LLM_UNAVAILABLE` | 500 | `LLM_PROVIDER` set but the provider call failed |
| `DATASET_NOT_FOUND` | 404 | Unknown dataset or data version |
| `DATA_VERSION_CONFLICT` | 409 | Version name already exists with different content |
| `CONFLICT` | 409 | `model_id` already onboarded (attach/onboard) |
| `CONFIGURATION_ERROR` / `ARTIFACT_ERROR` | 500 | Misconfiguration or artifact I/O failure |

### Data versioning and model endpoints

All routes are under `/api/v1`. Errors use the same `{"code", "message", "context"}` body as
the adaptation endpoint.

| Method and path | Purpose | Status codes |
|---|---|---|
| `POST /datasets` `{"dataset_id", "name"?, "description"?}` | Create a dataset (get-or-create) | 201 |
| `GET /datasets` | List datasets with their version names | 200 |
| `POST /datasets/{id}/versions` | Ingest a version. Body: `version`, `records` (at least 1 row), and optionally `kind` (`HISTORICAL`/`DRIFTED`), `timestamp_column` *or* `start`, `parent_version`, `model_id` + `model_version` + `role`, `source` | 201 when created, 200 for an identical replay, 409 `DATA_VERSION_CONFLICT`, 404 for an unknown model, 422 for an invalid body |
| `GET /datasets/{id}/versions` | List the versions (hash, rows, columns, time range, parent) | 200, or 404 `DATASET_NOT_FOUND` |
| `GET /datasets/{id}/versions/{v}` | Show one version | 200, or 404 |
| `GET /datasets/{id}/versions/{v}/lineage` | Show the version, its ancestor chain, and the model versions linked to it | 200, or 404 |
| `GET /models` | List onboarded models | 200 |
| `GET /models/{id}` | Show metadata, data links, all MLflow versions with their tags and aliases, `live_alias`, and `live_version` | 200, or 404 `MODEL_NOT_FOUND` |
| `POST /models/attach` | Adopt a model version already in MLflow. Body: `model_id`, `mlflow_model_name`, `framework`, `task_type`, `target_column`, and optionally `version`, `dataset_id`, `training_version` | 201, 409 `CONFLICT`, or 404 |

No endpoint accepts model file uploads, because loading an uploaded pickle would execute
arbitrary code. Use `onboard_model()` or the CLI with a trusted local file instead.

---

## 6. Seeding a model to test against

`/adaptation/events` needs a model already registered (a `ModelMetadata` row plus an MLflow
model version aliased `live`) before it can do anything but `NO_ACTION`. Two ready-made ways to
get one:

- **`python scripts/demo.py`** — seeds `demo-cell-classifier` into its own `data/demo/` store
  and exercises it immediately (see §4). Good for "just show me it working."
- **Write your own seed script** modeled on `scripts/demo.py`'s `_seed_model()` — train/log a
  model with `mlflow.sklearn.log_model(...)`, alias it `live` via
  `MlflowRegistry.set_alias(...)`, and insert a `ModelMetadata` row plus historical/drifted
  `DataVersion`/`DataRecord` rows pointing at the same `model_id`, against whatever
  `DATABASE_URL`/`MLFLOW_TRACKING_URI` your running server is using.
- **`oran_adapt.registry.onboarding.onboard_model(...)`** is the supported programmatic path.
  In one call it:
  - validates the target column,
  - logs the model to MLflow with `data.*` tags,
  - aliases it `live`,
  - writes the `ModelMetadata` row,
  - ingests the training data (and, optionally, the drifted data) as immutable data versions
    linked to model version 1.

  `attach_existing_model(...)` adopts a version that is already in MLflow.
- **CLI:** the `oran-adapt` command (also `python -m oran_adapt.cli`) exposes the same operations:
  - `oran-adapt data ingest --dataset kpi --version v1 --csv train.csv`
  - `oran-adapt model onboard --model-id m1 --model-file m1.joblib --framework sklearn --task-type regressor --target y --dataset kpi --training-csv train.csv`
  - `oran-adapt model show --model-id m1`
  - `oran-adapt event submit --model-id m1 --dataset kpi --drifted-version v2`

  `--model-file` is unpickled, so only pass files you trust.

  Row timestamps are part of a version's content hash. If the CSV has no timestamp column,
  pass `--start 2026-01-01T00:00:00+00:00` to `data ingest` (or use `--timestamp-column`).
  Otherwise the rows are stamped from "now", and re-ingesting the same file is reported as
  `DATA_VERSION_CONFLICT` instead of being treated as a no-op.

### Data versioning model

A data version is immutable. Its identity is its SHA-256 `content_hash`, computed over the
canonically ordered columns, the values, and the timestamps.

- Re-ingesting identical content under the same version name is an idempotent no-op.
- Ingesting different content under that name raises `DATA_VERSION_CONFLICT` (409).

Versions carry a `parent_version`, which gives each version an ancestor chain. `ModelDataAssociation` rows link a
model version to data versions by role: `TRAINING`, `VALIDATION`, or `DRIFT_OBSERVED`.

After the pipeline registers a candidate, it does three things:

1. It snapshots the exact merged training set as `train-<model_id>-v<N>`. The parent version
   is the baseline it came from.
2. It links that snapshot to the new version as `TRAINING`, which makes it the baseline for
   the next drift cycle.
3. It adds these tags to the MLflow version:
   - `oran.model_id` / `oran.parent_version` / `oran.event_id`
   - `adaptation.strategy` / `adaptation.engine`
   - `validation.metric` / `validation.candidate_value` / `validation.current_value`
   - `data.source_versions` / `data.training_version` / `data.training_hash`

### Running a real MLflow server

`python scripts/run_mlflow_server.py [--port 5000]` serves tracking and the model registry
from `data/mlflow_server/`. It uses a `mlflow.db` SQLite backend and proxies artifacts through
`--serve-artifacts` into `artifacts/`, and it writes its log to `server.log`. Ctrl+C stops the
server together with its uvicorn worker. Set
`MLFLOW_TRACKING_URI=MLFLOW_REGISTRY_URI=http://127.0.0.1:5000` for the API and CLI.

---

## 7. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/oran_adapt.db` | App database (`postgresql+psycopg://...` in production) |
| `MLFLOW_TRACKING_URI` | `sqlite:///./data/mlflow.db` | MLflow tracking + registry store |
| `MLFLOW_REGISTRY_URI` | unset (falls back to tracking URI) | Only needed if registry and tracking store diverge |
| `ARTIFACT_WORKDIR` | `./data/artifacts` | Scratch dir for training jobs' working files |
| `LLM_PROVIDER` | `none` | `anthropic` \| `gemini` \| `none` |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | unset | Required when `LLM_PROVIDER` selects that provider |
| `ANTHROPIC_MODEL` / `GEMINI_MODEL` | `claude-sonnet-5` / `gemini-3.6-flash` | Model id used for LLM calls |
| `LLM_TIMEOUT_S` | `60` | Timeout for LLM calls |
| `SANDBOX_BACKEND` | `subprocess` | `subprocess` (restricted, resource-limited) \| `docker` (network-isolated container) |
| `SANDBOX_TIMEOUT_S` / `SANDBOX_MEMORY_MB` | `120` / `1024` | Limits on LLM-generated adaptation code |
| `SANDBOX_DOCKER_IMAGE` | `oran-adapt-sandbox:latest` | Image used when `SANDBOX_BACKEND=docker` |
| `MLFLOW_SKOPS_TRUSTED_TYPES` | `["sklearn.tree._tree.Tree", "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor"]` | Extra types skops may serialize and load for sklearn models. Anything else fails fast with a non-retryable `ARTIFACT_ERROR`. |
| `TORCH_FINE_TUNE_EPOCHS` / `TORCH_FULL_RETRAIN_EPOCHS` | `5` / `300` | Epochs for torch warm-start fine-tuning / from-scratch retraining |
| `TORCH_LEARNING_RATE` | `0.01` | Adam learning rate for both torch engines |
| `LIVE_ALIAS` | `live` | MLflow alias the pipeline promotes candidates to |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Logging verbosity/format |
| `JOB_MAX_RETRIES` / `JOB_RETRY_BACKOFF_S` | `2` / `1.0` | Retry policy for transient MLflow/DB errors |
| `JOB_TIMEOUT_S` | `600` | Wall-clock ceiling per adaptation job |
| `ANALYSIS_PSI_REUSE_THRESHOLD` / `ANALYSIS_KS_PVALUE_REUSE_THRESHOLD` / `ANALYSIS_DRIFT_SCORE_REUSE_THRESHOLD` | `0.1` / `0.05` / `0.3` | When Member 1 decides the existing model can just be reused |
| `DECISION_MIN_DRIFTED_ROWS` | `10` | Minimum drifted rows before any strategy is actionable |
| `DECISION_FULL_RETRAIN_PSI_THRESHOLD` | `0.5` | PSI above which fine-tuning is ruled out in favor of full retraining |
| `DECISION_SUPPORTED_FRAMEWORKS` | `["sklearn","xgboost","torch","pytorch"]` | Frameworks with a dedicated training engine |
| `VALIDATION_MIN_ROWS` | `5` | Minimum held-out rows to score a candidate at all |
| `VALIDATION_HOLDOUT_FRACTION` | `0.2` | Share of the newest drifted rows held back for validation and never trained on (at least `VALIDATION_MIN_ROWS`, always leaving one drifted row for training) |
| `VALIDATION_ACCURACY_TOLERANCE` | `0.02` | Max accuracy drop allowed for classifiers to pass |
| `VALIDATION_RMSE_TOLERANCE_RATIO` | `0.05` | Max RMSE increase (as a fraction of current RMSE) allowed for regressors to pass |

Full authoritative list: `src/oran_adapt/core/config.py`.

---

## 8. Tests and linting

```bash
pytest                      # full suite
pytest tests/unit           # unit tests only (fast, no external services)
ruff check src tests        # lint
```

Integration tests self-skip when their infrastructure isn't present:
- Postgres-only tests run when `TEST_DATABASE_URL` points at a real PostgreSQL instance.
- `tests/integration/test_docker_sandbox.py` runs when the `docker` CLI is on `PATH`.
- `tests/integration/test_docker_compose_e2e.py` additionally needs `RUN_DOCKER_E2E=1` and
  `POSTGRES_PASSWORD` set, since it builds and runs the whole compose stack.

Last verified run on this machine: **150 passed, 6 skipped** (see `docs/PHASE12_DEMO_DOCS.md`).

---

## 9. Running with Docker

```bash
docker compose up --build
```

Brings up PostgreSQL, an MLflow tracking server, and the API together (`docker-compose.yml` +
root `Dockerfile`).

The LLM-generated-code sandbox uses a separate image, built independently:

```bash
docker build -t oran-adapt-sandbox:latest -f docker/sandbox/Dockerfile docker/sandbox
```

Then set `SANDBOX_BACKEND=docker` in `.env` to route LLM-generated adaptation code through
`docker run --rm --network none --memory <n>m ...` instead of the restricted local subprocess
backend.

> **Not verified on this machine:** no Docker install is present here, so `docker build`,
> `docker run`, and the full compose stack have never actually executed in this repo's history.
> See `docs/PHASE11_DOCKER_E2E.md` for exactly what was and wasn't checked.

---

## 10. Independently verifying a run actually happened (not just trusting the API's own claims)

Useful after any manual test, to confirm state really changed on disk rather than the API
merely returning a plausible-looking response:

```bash
python - <<'EOF'
import sqlite3
con = sqlite3.connect("data/oran_adapt.db")          # or wherever DATABASE_URL points
con.row_factory = sqlite3.Row
for row in con.execute("SELECT id, model_id, status, strategy FROM adaptation_job ORDER BY id"):
    print(dict(row))
con.close()

con = sqlite3.connect("data/mlflow.db")               # or wherever MLFLOW_TRACKING_URI points
con.row_factory = sqlite3.Row
for row in con.execute("SELECT name, version, status FROM model_versions ORDER BY version"):
    print(dict(row))
for row in con.execute("SELECT name, alias, version FROM registered_model_aliases"):
    print(dict(row))
con.close()
EOF
```

And to prove a registered model is real and actually loadable/usable (not a stub file):

```bash
python - <<'EOF'
import mlflow
mlflow.set_tracking_uri("sqlite:///./data/mlflow.db")
mlflow.set_registry_uri("sqlite:///./data/mlflow.db")
model = mlflow.sklearn.load_model("models:/<your-mlflow-model-name>@live")
print(type(model), model)
EOF
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'oran_adapt'` running a bare `python` command | Package not installed editable in `.venv` | `pip install -e ".[dev]"`, or `export PYTHONPATH="$(pwd -W)/src"` for that one command |
| A `C:\c\...` folder appears outside the project after setting `DATABASE_URL`/`MLFLOW_TRACKING_URI` manually | Used `$(pwd)` (POSIX path) instead of `$(pwd -W)` (Windows path) inside a Git-Bash `sqlite:///` URL | Rebuild the URL with `pwd -W`; delete the stray folder once confirmed unused |
| `UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f3c3'` when registering against an `http://` MLflow server | MLflow prints an emoji "View run" link; a redirected stdout on Windows defaults to cp1252 | The demo and `oran-adapt` CLI switch stdout to UTF-8 themselves; for `uvicorn` or your own scripts set `PYTHONIOENCODING=utf-8` |
| `GET /api/v1/ready` returns `503` | Database or MLflow store unreachable — often just means migrations haven't run yet | Run the migration command in §2 |
| `POST /adaptation/events` returns `MODEL_NOT_FOUND` | No `ModelMetadata` row for that `model_id` | Seed one first — see §6 |
| Docker-related commands fail with "command not found" | No `docker` CLI installed on this machine | Expected here; those code paths are written and unit-tested but not integration-verified — see §9 |

---

## 12. Known limitations

These are real, code-verified gaps — not hypothetical — surfaced here so an operator reading
only this manual (not the source) still learns about them. See
`docs/IMPLEMENTATION_CHECKLIST.md`'s "Known gaps" section for how each was verified.

- **No enforced memory ceiling for the sandbox on Windows.** When `SANDBOX_BACKEND=subprocess`
  (the default), LLM-generated adaptation code runs with an enforced wall-clock timeout
  (`SANDBOX_TIMEOUT_S`) on every OS, but the memory ceiling (`SANDBOX_MEMORY_MB`) is only
  enforced on POSIX via `resource.setrlimit(RLIMIT_AS, ...)` — that syscall does not exist on
  Windows, so `_memory_limit_preexec()` in `src/oran_adapt/sandbox/runner.py` is a no-op there.
  A runaway or malicious LLM-generated script (already restricted by the AST safety scanner to
  a small import allowlist, but still capable of e.g. allocating a huge array) can therefore
  consume unbounded memory on a Windows host. The only way to get an actually-enforced memory
  limit today is `SANDBOX_BACKEND=docker` (see §9), which applies `--memory`/`--memory-swap` at
  the container level on every host OS — untested here for lack of a local Docker install.
- **MLflow global URI state during model logging and download.** MLflow's fluent
  `log_model` API, and its resolution of the nested `models:/m-<id>` sources it creates, read
  only the process-global tracking and registry URIs. `MlflowRegistry._fluent_uris()` therefore
  sets them for the duration of those calls and restores them exactly afterwards, including
  the `MLFLOW_*_URI` environment variables that MLflow's setters write. This is correct for
  sequential jobs and for concurrent jobs against the *same* MLflow server. It is not safe for
  concurrent jobs in one process that use *different* MLflow servers.
- **Training snapshots grow every cycle.** A snapshot is the full merged training set: the
  baseline plus all drifted rows. No windowing or down-sampling is applied, so storage and
  training time grow linearly with the number of adaptation cycles. The `overlap_count`
  reported by the merge is informational only.
- **A timed-out adaptation job's worker thread is not actually stopped.** Python has no API to
  forcibly kill a running thread. When a job exceeds `JOB_TIMEOUT_S`, the caller gets back a
  `JOB_TIMEOUT` error and the HTTP request returns immediately — but the abandoned worker thread
  keeps running in the background against its own database session, and can still write a late
  `COMPLETED`/`REGISTERED` result *after* the client has already been told the job failed. If you
  see a job whose `AdaptationJob.status` in the database doesn't match the `JOB_TIMEOUT` response
  your client received, this is why — check the job row directly (§10) rather than trusting only
  the synchronous response for jobs that timed out.
