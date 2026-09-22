# Phase 0 — Repository Audit and Implementation Plan

## 1. Existing-code assessment

| Item | Finding | Decision |
|---|---|---|
| Application source | None. Greenfield. | Build from scratch. |
| `headroom/` | Unrelated third-party clone (Rust/Python context compressor). | Do not touch, do not depend on; git-ignored. |
| `node_modules/`, `package.json` | Unofficial `claude` npm placeholder. | Irrelevant to the project; left alone. |
| `.claude/`, `skills-lock.json` | Dev-tool config. | Left alone. |
| Git | Not a repository. | Not initialised without user request. |

Nothing is reusable for Members 1–3, so nothing needs to be destroyed or migrated.

## 2. Dependency / environment assessment

| Need | Status |
|---|---|
| Python | 3.13.7 (>=3.11 satisfied) |
| fastapi, pydantic(-settings), httpx, pytest(-asyncio), sklearn, torch, xgboost, joblib | Present |
| mlflow 3.16, sqlalchemy 2.0, alembic, psycopg3, anthropic, google-genai, onnx, safetensors, ruff | Installed into `.venv` |
| **PostgreSQL** | **Not installed locally; `pgserver` has no Windows wheel.** |
| **Docker** | **Not installed.** |

Consequences (stated honestly, not hidden):

* Code targets PostgreSQL (`postgresql+psycopg`), but all SQLAlchemy models/Alembic migrations are
  written dialect-neutral so the automated test-suite runs against SQLite. Tests that need real
  Postgres are marked `integration` and run when `TEST_DATABASE_URL` points to one.
* Docker Compose, Dockerfile and the Docker sandbox backend will be written in full, but **cannot be
  executed on this machine**. Phase 11 verification will be reported as "files written, not run"
  unless Docker becomes available. The sandbox has a second, real backend (restricted subprocess with
  resource limits) that is executed in tests; it is weaker isolation and is documented as such.
* MLflow is exercised for real using a file/SQLite backed tracking store and the real Model Registry.

## 3. Architecture (single pipeline, internal modules)

```
POST /api/v1/adaptation/events
        │
        ▼
AdaptationOrchestrator  (creates/dedupes AdaptationJob in PostgreSQL)
  ├─ analysis/    Member 1  → AnalysisResult (reuse OR DecisionPackage)
  ├─ decision/    Member 2  → Decision (hard constraints → LLM → Pydantic-validated)
  ├─ adaptation/  Member 3  → CandidateModel (inspect → capability → engine registry
  │                                            → specific engine | LLM adapter in sandbox)
  ├─ validation/            → ValidationReport (V_current vs candidate)
  └─ registry/    MLflow    → register new version, alias `live`, rollback
```

Source of truth split: **MLflow** = models/versions/artifacts/aliases/metrics;
**PostgreSQL** = datasets, data versions, lineage, model↔data links, jobs, events, audit.

Package layout: `src/oran_adapt/{core,db,registry,analysis,decision,adaptation,validation,
orchestrator,llm,sandbox,api}`. Members are internal modules, not separate apps.

## 4. Phase plan

| # | Phase | Exit gate |
|---|---|---|
| 0 | Audit, scaffold | structure tests pass |
| 1 | Foundation: config, logging, schemas, DB, Alembic, MLflow client, health API, compose | start/DB/MLflow/health/migration tests |
| 2 | Member 1 | retrieval, timestamp merge, comparison, reuse, package |
| 3 | Member 2 | constraints, LLM validation/fallback |
| 4 | Member 3 core: inspector, loaders, schema/preproc/capability, registry | real sklearn + torch artifacts |
| 5 | Real retraining engines | train → save → reload → infer |
| 6 | Real PyTorch fine-tuning | proves warm start from existing weights |
| 7 | LLM adapters, AST security, sandbox | malicious code rejected |
| 8 | Validation | pass/fail paths |
| 9 | Orchestrator + 3 E2E scenarios | |
| 10 | Hardening | idempotency, concurrency, retries, timeouts |
| 11 | Docker E2E | blocked locally unless Docker present |
| 12 | Demo scripts, README, docs | |

Each phase ends with the mandated `PHASE STATUS` block.
