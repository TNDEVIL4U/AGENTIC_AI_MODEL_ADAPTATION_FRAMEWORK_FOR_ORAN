# Project Status Report — Agentic AI Model Adaptation Framework for O-RAN

**Date:** 2026-09-23 · **Branch:** `phase13-registry-data-versioning` (pushed to GitHub, not yet merged to `main`)

This report answers four questions: what is done, what is left, how much is complete, and
whether everything is real or hardcoded. It is based on the code as it is now, a fresh run of
the multi-model demo today (16/16 checks passed), a new CLI end-to-end test (2/2 passed), and
a targeted audit of the source for hardcoded values. The earlier, more detailed phase audit
is in `docs/IMPLEMENTATION_CHECKLIST.md`.

---

## 1. Headline

| Measure | Result |
|---|---|
| Code written | **13 of 13 phases (100%)** — no stubs, no `NotImplementedError`, no fake results in `src/` |
| Verified working | **≈ 88%** (11.5 of 13 phase-points, see §3 for the formula) |
| Demo run today | **16/16 checks passed**, exit code 0, about 2 min, CPU only — re-run after the validation fix |
| Tests in the repo | Full suite run after the fix: **163 passed, 6 skipped** (the skipped ones need Docker) in 5 min |
| Fixed today | **Validation now uses a real hold-out** (it used to score on training rows — see §4, item 1) |

**Short answer:** the framework really works end to end — real statistics, real training,
real MLflow registration, real data versioning, and (since today's fix) validation on data
the new model never trained on. What is left is two paths that could not be run on this
laptop (Docker, the LLM decision call), a few small improvements, and some scope questions
(no live O-RAN interface, synthetic data only).

---

## 2. What is done

| Phase | What it does | Status | How it was verified |
|---|---|---|---|
| 1 — Foundation | Settings from `.env`, logging, DB models, Alembic migrations, MLflow client, health/ready endpoints | ✅ Done | Unit tests; `/api/v1/ready` → 200 in today's demo |
| 2 — Analysis | Loads historical + drifted data, KS test and PSI per feature, decides "reuse" vs "adapt" | ✅ Done | Unit tests; demo: `kpi-stable-sgd` reused, second cycle detects shift |
| 3 — Decision | Hard constraints (min rows, framework support, PSI cut-off), LLM strategy choice, rule fallback | 🟡 Partly | Rules and fallback verified; live LLM **decision** call never returned a success (see §4) |
| 4 — Adaptation: inspection | Detects framework, estimator type, `partial_fit` / warm-start support | ✅ Done | Unit tests |
| 5 — Adaptation: sklearn/xgboost engines | `partial_fit` fine-tuning, clone-and-refit full retraining | ✅ Done | Unit tests; demo: SGD, Ridge, RandomForest, XGBoost all REGISTERED |
| 6 — Adaptation: torch engine | Warm-start fine-tune vs reset-and-retrain | ✅ Done | Unit tests; demo: both torch models REGISTERED |
| 7 — Sandbox | AST security scanner + subprocess runner for LLM-generated code | 🟡 Partly | Scanner and subprocess runner verified; Docker backend not run; memory limit not enforced on Windows |
| 8 — Validation | Compares candidate vs current model (accuracy / RMSE gate) on a time-based hold-out | ✅ Done | Gate maths verified; hold-out fixed today and tested (40 unseen rows, 360-row training snapshot) |
| 9 — Orchestrator | analyze → decide → adapt → validate → register, one drift event end to end | ✅ Done | Unit tests; demo sections 3 and 6 |
| 10 — Hardening | Idempotent events, concurrency, selective retries, job timeout | ✅ Done | Unit tests incl. a real thread race; demo section 4 (`duplicate=True`) |
| 11 — Docker sandbox + compose | Dockerfiles, compose file, Docker runner | 🟡 Partly | Code and command building unit-tested; never run (no Docker on this laptop) |
| 12 — Demo and docs | `scripts/demo.py`, README, `MANUAL.md`, now `DEMO_MANUAL.md` | ✅ Done | Demo re-run today |
| 13 — Registry + data versioning | Content-hashed immutable data versions, lineage, MLflow tags, onboarding, data/model HTTP API, `oran-adapt` CLI, real MLflow server script | ✅ Done | 10 unit tests + 2 CLI end-to-end tests; demo sections 2, 5, 6 |

Also finished this session: the CLI end-to-end test (`tests/unit/test_phase13_cli.py`) and a
CLI fix — `data ingest --start` — so re-ingesting the same CSV without a timestamp column is a
no-op instead of a false `DATA_VERSION_CONFLICT`.

---

## 3. How the percentage was calculated

13 phase-points (Phases 1–13, counting 4, 5 and 6 separately). A phase that is fully
verified counts 1, a partly verified phase counts 0.5:

- Fully verified: 1, 2, 4, 5, 6, 8, 9, 10, 12, 13 → **10**
- Partly verified: 3, 7, 11 → 3 × 0.5 = **1.5**
- Total: **11.5 / 13 ≈ 88%**

This measures the framework as it was specified in this repo. It does **not** include the
scope items in §4 C (live O-RAN integration, real network data), which were never part of the
implemented plan. If those are required for your project, the percentage should be lower —
decide that scope first.

---

## 4. What is still to be done

### A. Defects and gaps in the current code (priority order)

1. **FIXED today — validation now uses a real hold-out.** Before the fix, the pipeline
   trained on historical + drifted data and then validated on those same drifted rows, so
   scores were optimistic. Now the newest `VALIDATION_HOLDOUT_FRACTION` (default 0.2) of the
   drifted rows are held back: at least `VALIDATION_MIN_ROWS`, always leaving one drifted
   row to train on. The candidate trains on the rest, both models are scored only on the
   held-back rows, and the training snapshot excludes them (the demo's snapshots went from 400
   to 360 rows, with an MLflow `validation.holdout_rows=40` tag). Files: `core/config.py`,
   `adaptation/data.py`, `orchestrator/pipeline.py`, `datastore/versioning.py`. Verified by the
   Phase 5, 8, 9, 10 and 13 test files (45 passed) and a demo re-run (16/16).
2. **LLM decision call not verified live.** `decision/llm_selector.py` was tried against real
   Gemini but only ever got 404 / 429 / 503 from Google. The error handling and fallback are
   proven; a successful 2xx response is not. **Needs:** a retry with a working
   `GEMINI_MODEL` (or an Anthropic key).
3. **Docker sandbox never executed.** Code and tests exist and self-skip. **Needs:** Docker
   Desktop installed.
4. **Sandbox memory limit is not enforced on Windows** (`RLIMIT_AS` is POSIX-only). Disclosed
   in README and MANUAL. Only fixable by using the Docker backend (item 3) or a Windows Job
   Object.
5. **Timed-out jobs keep running in the background thread** (Python cannot kill a thread).
   Disclosed in README and MANUAL; the job status in the DB is the source of truth.

### B. Small improvements

6. **Done — torch learning rate is now a setting:** `TORCH_LEARNING_RATE` (default `0.01`),
   passed to both torch engines; covered by a new test in `test_phase6_torch.py`.
7. **Done — default `GEMINI_MODEL` is now `gemini-3.6-flash`**, the model that worked for your
   account (the old `gemini-2.5-pro` returned 404). Also updated in `.env.example` and MANUAL.
8. **Done — full test suite re-run after the fix:** 163 passed, 6 skipped (Docker-only).
9. **Done — housekeeping:** `*.egg-info/` is in `.gitignore`, and the Phase 13 branch is pushed
   to GitHub, ready for a pull request into `main` (opening and merging it is your call).

### C. Scope questions (not started — confirm whether they are required)

10. **No live O-RAN interface.** There is no Near-RT/Non-RT RIC, xApp/rApp, A1, E2 or O1 code.
    Drift events arrive through the framework's own HTTP API (`POST /api/v1/adaptation/events`)
    and the CLI. Deploying as an rApp/xApp would be new work.
11. **Only synthetic data has been used.** The demo and tests generate KPI-like data
    (`prb_util`, `cqi`, `rsrp`) with NumPy. Nothing has been run on a real O-RAN dataset yet.
12. **No drift detector of its own on live streams.** The framework reacts to a drift event
    (and re-checks it with KS/PSI); something upstream must send that event.

---

## 5. Is everything real, or are there hardcoded values?

### Real (not faked, not hardcoded)

| Part | Evidence |
|---|---|
| Drift statistics | Real `scipy` two-sample KS test and the standard PSI formula on the actual data |
| Training | Real `partial_fit`, clone-and-refit, XGBoost fit, torch training loops — the demo's v2 models give different predictions and metrics from v1 |
| Registration | Real MLflow model versions, `live` alias moves, tags; models are downloaded back and asked for predictions in the demo |
| Data versioning | Real SHA-256 content hashes; identical re-ingest returns 200, changed content with the same name returns 409 |
| Validation | Real accuracy / RMSE on the newest 20% of the drifted rows, which the candidate never trains on |
| Idempotency / retries / timeout | Enforced by DB constraints and real retry/timeout code, tested with real threads |
| LLM code generation path | Verified live against Gemini in an earlier session (real network call, generated code ran in the sandbox, model registered) |
| Tests | Use real SQLite and real MLflow stores; the only stand-in is `FakeLlmClient` for LLM calls, which is a test double, not used in the app |

### Configurable values (set in `.env`, defaults in `core/config.py`)

These are **defaults, not hardcoded** — each can be changed without editing code:

| Setting | Default | Meaning |
|---|---|---|
| `ANALYSIS_PSI_REUSE_THRESHOLD` | 0.1 | PSI above this = feature shifted |
| `ANALYSIS_KS_PVALUE_REUSE_THRESHOLD` | 0.05 | KS p-value below this = feature shifted |
| `ANALYSIS_DRIFT_SCORE_REUSE_THRESHOLD` | 0.3 | Caller drift score that forces adaptation |
| `DECISION_MIN_DRIFTED_ROWS` | 10 | Fewer rows → `INSUFFICIENT_INFORMATION` |
| `DECISION_FULL_RETRAIN_PSI_THRESHOLD` | 0.5 | PSI at/above this rules out fine-tuning |
| `VALIDATION_MIN_ROWS` | 5 | Minimum rows to validate at all |
| `VALIDATION_HOLDOUT_FRACTION` | 0.2 | Share of the newest drifted rows held back for validation |
| `VALIDATION_ACCURACY_TOLERANCE` | 0.02 | Allowed accuracy drop for classifiers |
| `VALIDATION_RMSE_TOLERANCE_RATIO` | 0.05 | Allowed relative RMSE rise for regressors |
| `TORCH_FINE_TUNE_EPOCHS` / `TORCH_FULL_RETRAIN_EPOCHS` | 5 / 300 | Torch training budgets |
| `TORCH_LEARNING_RATE` | 0.01 | Adam learning rate for both torch engines |
| `JOB_MAX_RETRIES` / `JOB_RETRY_BACKOFF_S` / `JOB_TIMEOUT_S` | 2 / 1.0 / 600 | Job hardening |
| `SANDBOX_TIMEOUT_S` / `SANDBOX_MEMORY_MB` | 120 / 1024 | Sandbox limits |
| `MLFLOW_SKOPS_TRUSTED_TYPES` | two sklearn tree types | Extra types allowed when saving sklearn models |

Note: `TORCH_FULL_RETRAIN_EPOCHS=300` was raised from 30 so the demo's torch full-retrain
case passes validation. It is a tuned default, not a universal value.

### Hardcoded in the code (cannot be changed without editing code)

| Value | Where | Assessment |
|---|---|---|
| PSI uses 10 bins, `1e-6` floor | `analysis/comparison.py` | Standard PSI practice; fine, could be a setting |
| Fallback decision `confidence=0.5`, hard-constraint `confidence=1.0` | `decision/engine.py` | Fixed labels only reported in the result; they do not affect any decision |
| Fallback priority order: fine-tuning → full retraining → rollback → no action | `decision/fallback.py` | Deliberate deterministic rule ("cheapest compatible first") |
| LLM system prompts, sandbox allowed-imports list | `decision/llm_selector.py`, `adaptation/llm_adapter.py`, `sandbox/security.py` | Deliberate, part of the design |

### Hardcoded in the demo only (by design)

`scripts/demo_models.py` uses fixed random seeds, fixed shift sizes (0.4 mild, 3.0 strong),
200 rows per dataset, and a list of **expected** outcomes per model. The expectations are used
only to check the results and print PASS/FAIL. They do not force the pipeline's answers: if
the pipeline chose differently, the demo would print FAIL and exit 1.

---

## 6. Recommended next steps

1. Open a pull request from `phase13-registry-data-versioning` and merge it into `main`.
2. When available: retry the live LLM decision call; install Docker to close Phase 11.
3. Decide whether live O-RAN integration and real datasets are in scope (§4 C).
