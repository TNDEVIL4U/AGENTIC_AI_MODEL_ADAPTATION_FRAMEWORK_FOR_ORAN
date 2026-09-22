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
| `result` | A `JobResult` — `outcome` is `NO_ACTION`, `REGISTERED`, or `REJECTED`, plus the full decision/candidate/validation trail |
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
| `CONFIGURATION_ERROR` / `ARTIFACT_ERROR` | 500 | Misconfiguration or artifact I/O failure |

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
| `ANTHROPIC_MODEL` / `GEMINI_MODEL` | `claude-sonnet-5` / `gemini-2.5-pro` | Model id used for LLM calls |
| `LLM_TIMEOUT_S` | `60` | Timeout for LLM calls |
| `SANDBOX_BACKEND` | `subprocess` | `subprocess` (restricted, resource-limited) \| `docker` (network-isolated container) |
| `SANDBOX_TIMEOUT_S` / `SANDBOX_MEMORY_MB` | `120` / `1024` | Limits on LLM-generated adaptation code |
| `SANDBOX_DOCKER_IMAGE` | `oran-adapt-sandbox:latest` | Image used when `SANDBOX_BACKEND=docker` |
| `LIVE_ALIAS` | `live` | MLflow alias the pipeline promotes candidates to |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Logging verbosity/format |
| `JOB_MAX_RETRIES` / `JOB_RETRY_BACKOFF_S` | `2` / `1.0` | Retry policy for transient MLflow/DB errors |
| `JOB_TIMEOUT_S` | `600` | Wall-clock ceiling per adaptation job |
| `ANALYSIS_PSI_REUSE_THRESHOLD` / `ANALYSIS_KS_PVALUE_REUSE_THRESHOLD` / `ANALYSIS_DRIFT_SCORE_REUSE_THRESHOLD` | `0.1` / `0.05` / `0.3` | When Member 1 decides the existing model can just be reused |
| `DECISION_MIN_DRIFTED_ROWS` | `10` | Minimum drifted rows before any strategy is actionable |
| `DECISION_FULL_RETRAIN_PSI_THRESHOLD` | `0.5` | PSI above which fine-tuning is ruled out in favor of full retraining |
| `DECISION_SUPPORTED_FRAMEWORKS` | `["sklearn","xgboost","torch","pytorch"]` | Frameworks with a dedicated training engine |
| `VALIDATION_MIN_ROWS` | `5` | Minimum held-out rows to score a candidate at all |
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
- **A timed-out adaptation job's worker thread is not actually stopped.** Python has no API to
  forcibly kill a running thread. When a job exceeds `JOB_TIMEOUT_S`, the caller gets back a
  `JOB_TIMEOUT` error and the HTTP request returns immediately — but the abandoned worker thread
  keeps running in the background against its own database session, and can still write a late
  `COMPLETED`/`REGISTERED` result *after* the client has already been told the job failed. If you
  see a job whose `AdaptationJob.status` in the database doesn't match the `JOB_TIMEOUT` response
  your client received, this is why — check the job row directly (§10) rather than trusting only
  the synchronous response for jobs that timed out.
