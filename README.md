# Agentic AI Model Adaptation Framework for O-RAN Architecture

An agentic pipeline that reacts to drift-detection events from O-RAN components, decides
whether and how a deployed model needs to be adapted, carries out that adaptation (reuse,
fine-tuning, full retraining, or LLM-generated code run in a sandbox), validates the
candidate against the currently-live model, and — if it passes — registers and promotes it
in MLflow.

Given a `POST /api/v1/adaptation/events` drift notification for a `model_id`, the pipeline:

1. **Analyzes** drift (Member 1) — compares historical vs. drifted data (PSI, KS test) and
   decides whether the existing model can simply be reused, or produces a `DecisionPackage`.
2. **Decides** a strategy (Member 2) — applies hard constraints (enough drifted rows? a
   supported framework?) and an LLM-assisted, Pydantic-validated decision between
   `FINE_TUNING`, `FULL_RETRAINING`, and unsupported-framework fallback.
3. **Adapts** the model (Member 3) — routes to a real training engine (sklearn / XGBoost /
   PyTorch) for known frameworks, or to an LLM-generated adaptation script executed inside a
   sandbox (restricted subprocess or Docker) for anything else.
4. **Validates** the candidate — scores it against the live model on held-out data and only
   proceeds if it's within tolerance (accuracy for classifiers, RMSE ratio for regressors).
5. **Registers** the candidate in MLflow and moves the `live` alias to it, or rolls back and
   reports `NO_ACTION` / a validation failure.

Every step is idempotent (`DriftEvent.idempotency_key()` + a unique DB constraint), retried
on transient MLflow/PostgreSQL outages, and bounded by a wall-clock job timeout — see
`docs/PHASE0_AUDIT.md` for the full phase-by-phase build history and
`docs/PHASE11_DOCKER_E2E.md` for the Docker sandbox backend's status.

## Architecture

Source of truth split: **MLflow** owns models, versions, artifacts, aliases and metrics;
**PostgreSQL** (SQLite locally) owns datasets, data versions, lineage, model↔data
associations, jobs, drift events and audit history.

```
POST /api/v1/adaptation/events
        │
        ▼
AdaptationOrchestrator  (creates/dedupes AdaptationJob in the database)
  ├─ analysis/    Member 1  → AnalysisResult (reuse OR DecisionPackage)
  ├─ decision/    Member 2  → Decision (hard constraints → LLM → Pydantic-validated)
  ├─ adaptation/  Member 3  → CandidateModel (inspect → capability → engine registry
  │                                            → specific engine | LLM adapter in sandbox)
  ├─ validation/            → ValidationReport (V_current vs candidate)
  └─ registry/    MLflow    → register new version, alias `live`, rollback
```

Package layout (`src/oran_adapt/`):

```
core/           settings, schemas, enums, error types
db/             SQLAlchemy models, Alembic migrations, session/health helpers
registry/       MLflow model-registry client
analysis/       Member 1 — drift analysis and reuse decision
decision/       Member 2 — strategy decision (constraints + LLM)
adaptation/     Member 3 — training engines, LLM adapter
validation/     candidate-vs-live scoring gate
orchestrator/   job submission, idempotency, retries, timeouts, pipeline wiring
llm/            Anthropic / Gemini client wrappers
sandbox/        subprocess and Docker execution backends for LLM-generated code
api/            FastAPI app, routes, ASGI entrypoint
```

## Requirements

- Python ≥ 3.11 (developed against 3.13)
- PostgreSQL, for production use (SQLite is used automatically for local dev/tests)
- Docker, optional — only needed for `SANDBOX_BACKEND=docker` and the docker-compose stack
- An Anthropic or Gemini API key, optional — only needed for the LLM-generated fallback
  adaptation path and the LLM-assisted decision step

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

Key variables (all have safe local defaults except where noted — see
`src/oran_adapt/core/config.py` for the complete, authoritative list):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/oran_adapt.db` | App database (use `postgresql+psycopg://...` in production) |
| `MLFLOW_TRACKING_URI` | `sqlite:///./data/mlflow.db` | MLflow tracking + registry store |
| `LLM_PROVIDER` | `none` | `anthropic` \| `gemini` \| `none` — required for LLM decisions/fallback adaptation |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | unset | Required when `LLM_PROVIDER` selects that provider |
| `SANDBOX_BACKEND` | `subprocess` | `subprocess` (restricted, resource-limited) \| `docker` (network-isolated container) |
| `SANDBOX_TIMEOUT_S` / `SANDBOX_MEMORY_MB` | `120` / `1024` | Limits applied to LLM-generated adaptation code |
| `SANDBOX_DOCKER_IMAGE` | `oran-adapt-sandbox:latest` | Image used when `SANDBOX_BACKEND=docker` (see `docker/sandbox/Dockerfile`) |
| `JOB_MAX_RETRIES` / `JOB_RETRY_BACKOFF_S` | `2` / `1.0` | Retry policy for transient MLflow/DB errors |
| `JOB_TIMEOUT_S` | `600.0` | Wall-clock ceiling per adaptation job |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Logging verbosity and format |

Run database migrations:

```bash
python -c "from oran_adapt.db.migrate import upgrade_to_head; from oran_adapt.core.config import get_settings; upgrade_to_head(get_settings().database_url)"
```

Start the API:

```bash
uvicorn oran_adapt.api.main:app --reload
```

## Try it: the demo script

`scripts/demo.py` is a runnable, self-contained walkthrough of the pipeline — no Docker, no
PostgreSQL, and no LLM API key required. It seeds a registered model plus historical and
drifted data into local SQLite-backed stores, then drives the real FastAPI app (the same one
`uvicorn` serves) through `TestClient`:

```bash
python scripts/demo.py
```

It exercises:

- a readiness check (`GET /api/v1/ready`),
- a **no-drift** event → `NO_ACTION`,
- a **genuine-drift** event → `FULL_RETRAINING` → validated → registered as version 2, with
  the `live` MLflow alias moved to it,
- resubmitting the same event → the idempotency path (`duplicate: true`, same `job_id`).

The one scenario it can't cover without a real API key is LLM-generated fallback adaptation
(for frameworks with no dedicated training engine) — set `LLM_PROVIDER` and the matching API
key in `.env` to try that path; see `tests/unit/test_phase9_orchestrator.py`'s third scenario
for the exact request shape that triggers it.

### Multi-model demo against a real MLflow server

`scripts/demo_models.py` onboards eight models covering sklearn (Ridge, RandomForest, SGD
`partial_fit`), xgboost, and torch. It fires a drift event at each one and then checks the
following:

- idempotency,
- the lineage of every registered version: MLflow tags, training-data snapshots, and the
  model↔data links,
- a second adaptation cycle. That cycle ingests new drifted data through the data API and
  confirms that the previous cycle's snapshot is used as the new baseline.

```bash
python scripts/demo_models.py --start-server   # starts, uses, and stops a local MLflow server
python scripts/demo_models.py                  # uses a throwaway SQLite MLflow store instead
```

## Model registry and data versioning

- **MLflow server:** `python scripts/run_mlflow_server.py [--port 5000]` runs a real tracking
  and model-registry server under `data/mlflow_server/`. It uses a SQLite backend and a
  proxied artifact store, and it stops cleanly on Ctrl+C. To use it, set
  `MLFLOW_TRACKING_URI=MLFLOW_REGISTRY_URI=http://127.0.0.1:5000`.
- **Data versioning (built in, with no DVC):** each dataset version is stored immutably in
  the database and carries a SHA-256 content hash.
  - Re-ingesting identical content is a no-op. Ingesting *different* content under an
    existing version name returns `409 DATA_VERSION_CONFLICT`.
  - Versions record their parent version, and models are linked to data versions by role:
    `TRAINING`, `VALIDATION`, or `DRIFT_OBSERVED`.
- **Pipeline integration:** each registered candidate version gets these MLflow tags:
  - `oran.*`, `adaptation.*`, `validation.*`
  - `data.source_versions`, `data.training_version`, `data.training_hash`

  The exact data the candidate was trained on is also snapshotted as a new version,
  `train-<model_id>-v<N>`. That version is linked as the model's `TRAINING` data, so it
  becomes the baseline for the next drift cycle.
- **Tree models:** `MLFLOW_SKOPS_TRUSTED_TYPES` lists the skops types that are allowed to be
  serialized. By default it covers sklearn trees and hist-gradient-boosting predictors.
  Anything outside the list fails fast with a non-retryable `ARTIFACT_ERROR`.
- **CLI:** the `oran-adapt` command (or `python -m oran_adapt.cli`) has these subcommands:
  - `db upgrade`
  - `data create-dataset|ingest|list|lineage`
  - `model onboard|attach|show`
  - `event submit`

## API

- `POST /api/v1/adaptation/events` — submit a `DriftEvent` (`model_id`, `event_id`,
  `drift_detected`, plus optional drift metrics). Returns a `JobResponse` (`job_id`, `status`,
  `strategy`, `result`, `duplicate`). Responds `201` for a newly created job, `200` when the
  `event_id` was already processed (idempotent replay).
- `GET /api/v1/health` — liveness only; does not touch dependencies.
- `GET /api/v1/ready` — readiness; checks the database and MLflow are both reachable, `503`
  if either is down.
- `POST/GET /api/v1/datasets` — create or list datasets.
- `POST /api/v1/datasets/{id}/versions` — ingest a data version from JSON records. Returns
  `201` when the version is new, `200` for an identical replay, and `409` for conflicting
  content.
- `GET /api/v1/datasets/{id}/versions[/{version}[/lineage]]` — list versions, show one
  version, or show its lineage.
- `GET /api/v1/models`, `GET /api/v1/models/{id}` — onboarded models. Each model's view
  includes its MLflow versions and tags, its live version, and its data links.
- `POST /api/v1/models/attach` — adopt a model version that is already in MLflow. There is no
  upload endpoint, because model files are pickles.

## Testing

```bash
pytest
```

Tests use a real SQLite database and a real file/SQLite-backed MLflow instance — the only
mocked boundary is the LLM provider client (`FakeLlmClient`). Tests that need real
infrastructure are marked `integration` and skip themselves cleanly when that infrastructure
isn't present:

- Postgres-only tests run when `TEST_DATABASE_URL` points at a real PostgreSQL instance.
- Docker sandbox tests (`tests/integration/test_docker_sandbox.py`) run when the `docker` CLI
  is on `PATH`.
- The full docker-compose E2E test (`tests/integration/test_docker_compose_e2e.py`) additionally
  requires `RUN_DOCKER_E2E=1` and `POSTGRES_PASSWORD` to be set, since it builds and runs the
  entire stack.

Lint:

```bash
ruff check src tests
```

## Running with Docker

```bash
docker compose up --build
```

This brings up PostgreSQL, an MLflow tracking server, and the API (see `docker-compose.yml`
and the root `Dockerfile`). The LLM-generated-code sandbox has its own image, built
separately:

```bash
docker build -t oran-adapt-sandbox:latest -f docker/sandbox/Dockerfile docker/sandbox
```

then set `SANDBOX_BACKEND=docker` in `.env` to route LLM-generated adaptation code through
containers (`--network none`, memory-limited, no host access) instead of the restricted local
subprocess backend.

**Honesty note:** this project was developed on a machine without Docker installed. The
Docker sandbox backend, the sandbox Dockerfile, and the compose stack are written in full and
covered by integration tests, but those tests have never actually executed here — they skip
themselves for the documented reason. See `docs/PHASE11_DOCKER_E2E.md` for exactly what was
and wasn't verified.

## Known limitations

- **Windows: no enforced sandbox memory ceiling.** `SANDBOX_MEMORY_MB` is only enforced on
  POSIX (`resource.setrlimit`); on Windows the subprocess sandbox backend has a real wall-clock
  timeout but no memory cap. Use `SANDBOX_BACKEND=docker` for an enforced limit on any OS.
- **A timed-out job's worker thread keeps running.** Python cannot forcibly kill a thread, so
  past `JOB_TIMEOUT_S` the caller is unblocked but the abandoned worker may still write a late
  result to the database after the client was told it failed — check `AdaptationJob.status`
  directly for jobs that timed out.
- **Concurrent jobs share MLflow's global URI state during model logging and download.**
  MLflow's fluent `log_model` API and its `models:/` URI resolution only read the global
  tracking and registry URIs. The registry client therefore sets those URIs for the duration
  of each call and restores them exactly afterwards. This is not safe when jobs run in
  parallel against *different* MLflow servers in the same process.
- **Training snapshots grow on every cycle.** Each snapshot is the full merged training set
  (the previous baseline plus the drifted data). No windowing or down-sampling is applied.

See `docs/MANUAL.md` §12 and `docs/IMPLEMENTATION_CHECKLIST.md`'s "Known gaps" for details.

## Manual

`docs/MANUAL.md` is the complete operator reference: every setup/run/test/lint/Docker command,
the full API endpoint reference (request/response schemas, status codes, error codes), the
environment variable table, how to seed a test model, how to independently verify a run actually
happened (not just trust the API's response), and a troubleshooting table for issues hit while
developing this on Windows/Git-Bash.

## Project history

- `docs/PHASE0_AUDIT.md` — the original repository audit and the 13-phase build plan this
  project was implemented against.
- `docs/PHASE11_DOCKER_E2E.md` — status of the Docker sandbox backend and compose E2E tests,
  including what remains unverified due to the lack of a local Docker install.
- `docs/PHASE12_DEMO_DOCS.md` — closing status for the final phase (demo script, README, docs),
  including the last full-suite and demo-script verification run.
