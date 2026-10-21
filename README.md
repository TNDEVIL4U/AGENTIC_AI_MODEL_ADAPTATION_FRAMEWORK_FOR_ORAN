# Agentic AI Model Adaptation Framework for O-RAN

**Paper title:** *Agentic AI-Based Model Regeneration Framework for O-RAN*  
**Course:** CS23502 — Networks and Data Communication, Mini Project  
**Repository:** <https://github.com/TNDEVIL4U/AGENTIC_AI_MODEL_ADAPTATION_FRAMEWORK_FOR_ORAN>

A deployed O-RAN prediction model, such as cell throughput from PRB utilisation, CQI and
RSRP, degrades as the network changes. When the framework receives a drift or degradation
event for that model, it runs these steps:

1. It confirms the model has really degraded.
2. It checks whether an already-registered version fits the new data.
3. Otherwise it decides how to adapt the model: fine-tune, fully retrain, or LLM-generated
   code run in a sandbox.
4. It runs that adaptation.
5. It validates the candidate against the live model with a statistical gate.
6. Only then does it register the candidate in MLflow and deliver it (shadow, canary, A/B,
   manual approval or blue-green), verifying what the serving side actually reports.

Each step is recorded in an audit trail.

This README has two parts. **Part A** is the procedure to install, run and check the
project. **Part B** explains how the files link together.

---

## Team

| Name | Roll No. | Individual contribution | Code |
|---|---|---|---|
| **Tamilselvan R** | 2024503515 | Model Intake Agent and Independent Verification (KS-test confirmation) | [`api/routes_adaptation.py`](src/oran_adapt/api/routes_adaptation.py) · [`orchestrator/jobs.py`](src/oran_adapt/orchestrator/jobs.py) · [`registry/onboarding.py`](src/oran_adapt/registry/onboarding.py) · [`analysis/engine.py`](src/oran_adapt/analysis/engine.py) · [`analysis/retrieval.py`](src/oran_adapt/analysis/retrieval.py) · [`analysis/merge.py`](src/oran_adapt/analysis/merge.py) · [`analysis/comparison.py`](src/oran_adapt/analysis/comparison.py) · [`analysis/reuse.py`](src/oran_adapt/analysis/reuse.py) |
| **Nitesh N D** | 2024503529 | Similar Model Exchange and the historical dataset registry (PostgreSQL) | [`analysis/version_eval.py`](src/oran_adapt/analysis/version_eval.py) · [`analysis/reuse_decision.py`](src/oran_adapt/analysis/reuse_decision.py) · [`datastore/versioning.py`](src/oran_adapt/datastore/versioning.py) · [`datastore/access.py`](src/oran_adapt/datastore/access.py) · [`datastore/current_data.py`](src/oran_adapt/datastore/current_data.py) · [`db/models.py`](src/oran_adapt/db/models.py) · [`migrations/`](migrations/) · [`adapters/datasets_sql.py`](src/oran_adapt/adapters/datasets_sql.py) |
| **Ramanujam P** | 2024503525 | Agentic AI Decision Engine and the MLKTL Scenario Module (PSI severity) | [`decision/engine.py`](src/oran_adapt/decision/engine.py) · [`decision/constraints.py`](src/oran_adapt/decision/constraints.py) · [`decision/llm_selector.py`](src/oran_adapt/decision/llm_selector.py) · [`decision/fallback.py`](src/oran_adapt/decision/fallback.py) · [`decision/report.py`](src/oran_adapt/decision/report.py) · [`analysis/summary.py`](src/oran_adapt/analysis/summary.py) · [`llm/client.py`](src/oran_adapt/llm/client.py) |
| **Bhuvaneshwar T S** | 2024503565 | Validation & Registration Agent, deployment gate, experiments and evaluation | [`validation/engine.py`](src/oran_adapt/validation/engine.py) · [`validation/gate.py`](src/oran_adapt/validation/gate.py) · [`registry/publishing.py`](src/oran_adapt/registry/publishing.py) · [`registry/promotion.py`](src/oran_adapt/registry/promotion.py) · [`registry/deployment.py`](src/oran_adapt/registry/deployment.py) · [`delivery/controller.py`](src/oran_adapt/delivery/controller.py) · [`scripts/demo.py`](scripts/demo.py) · [`scripts/demo_models.py`](scripts/demo_models.py) · [`scripts/walkthrough.py`](scripts/walkthrough.py) |

**Shared by the team:**

- the Generic Model Adapters that run Finetune and Regenerate: [`adaptation/`](src/oran_adapt/adaptation/) and [`sandbox/`](src/oran_adapt/sandbox/);
- the orchestrator that joins the modules into Algorithm 1: [`orchestrator/pipeline.py`](src/oran_adapt/orchestrator/pipeline.py);
- the platform layers: [`api/`](src/oran_adapt/api/), [`core/`](src/oran_adapt/core/), [`adapters/`](src/oran_adapt/adapters/).

Module docstrings use the labels *Member 1–4* for the four pipeline stages (analysis,
decision, adaptation, validation). Those labels name stages, not people; the table above is
the team's allocation.

Every path in this README is a link to the file in the repository.

---

## At a glance

| | |
|---|---|
| Application code | 186 Python modules, about 33,000 lines, under [`src/oran_adapt/`](src/oran_adapt/) |
| Tests | 951 collected: 904 in the default gate tier, 336 in the fast `smoke` tier, 47 in the `heavy` tier (CI only) |
| REST API | 39 endpoints under `/api/v1` |
| CLI | `oran-adapt` with 11 command groups: `db`, `data`, `model`, `event`, `auth`, `config`, `jobs`, `rollout`, `worker`, `cdc`, `notifications` |
| Pluggable adapters | registries, deployment, model types, LLM providers, queues, auth, notifications, data sources; one entry-point group per port in [`pyproject.toml`](pyproject.toml) |
| Defaults | SQLite and a local MLflow store, no LLM, shadow delivery. It runs offline with no GPU and no Docker |

---

# PART A — Procedure: install, run and check

## A.1 Requirements

| Item | Requirement |
|---|---|
| Python | 3.12 or 3.13 recommended (3.11 works, but one conformance test needs 3.12+) |
| CPU / RAM | 2 cores and 4 GB RAM minimum; 4 cores and 8 GB recommended. No GPU |
| Disk | 5 GB free, mostly PyTorch |
| OS | Windows, macOS or Linux |
| Optional | PostgreSQL 14+ for production; Docker for the compose stack and the hard-limit sandbox; an Anthropic or Gemini key for the optional LLM paths |

## A.2 Install

```bash
git clone https://github.com/TNDEVIL4U/AGENTIC_AI_MODEL_ADAPTATION_FRAMEWORK_FOR_ORAN.git
cd AGENTIC_AI_MODEL_ADAPTATION_FRAMEWORK_FOR_ORAN

python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip

# CPU build of PyTorch, pinned versions from the lock file
pip install --extra-index-url https://download.pytorch.org/whl/cpu \
            -c requirements.lock -e ".[dev,security]"

cp .env.example .env                   # Windows: copy .env.example .env
```

> **Use the CPU PyTorch index.** The default CUDA wheel uses about 494 MB of memory on
> import. That pushes the sandbox tests past their 512 MB limit, and they fail for reasons
> unrelated to the code.

On Windows, [`scripts/run_local.ps1`](scripts/run_local.ps1) `setup` performs the same steps. Copy-paste versions of every step are in [`RUN.md`](RUN.md).

Confirm the installation:

```bash
python -c "import oran_adapt; print(oran_adapt.__version__)"     # 0.1.0
oran-adapt --help
```

## A.3 Prepare the database and an API key

```bash
python -m oran_adapt.cli db upgrade                     # create the schema (Alembic)
python -m oran_adapt.cli auth new-key --role ADMIN      # prints api_key and api_keys_entry
```

Then:

1. Put the printed `api_keys_entry` into `API_KEYS` in `.env`, for example
   `API_KEYS={"<digest>": "ADMIN"}`.
2. Send the printed `api_key` as the `X-API-Key` header. It is shown only once and stored
   nowhere.

## A.4 Check that it works — run these in order

| # | Command | Time | Expected result |
|---|---|---|---|
| 1 | `python scripts/walkthrough.py` ([`source`](scripts/walkthrough.py)) | ~30 s | 8 self-checking steps, ending `Walkthrough complete: the drift alert produced a candidate serving a canary slice.` |
| 2 | `python scripts/demo.py` ([`source`](scripts/demo.py)) | ~40 s | 15 checked stages, ending `Demo passed: all 15 stages checked.` |
| 3 | `python -m pytest -m smoke -q` | ~1.5 min | `336 passed` |
| 4 | `python -m pytest -q` | 15–20 min | the default gate tier (904 tests) passes; Postgres and Docker tests skip themselves when those are absent |
| 5 | `python -m ruff check src tests` | seconds | `All checks passed!` |
| 6 | `python scripts/security_check.py --skip-audit` ([`source`](scripts/security_check.py)) | 1–3 min | bandit, mypy and SBOM reports in `reports/` |
| 7 | `python -m oran_adapt.cli config lint config/examples/development.toml` ([`file`](config/examples/development.toml)) | seconds | `"ok": true` |

**What the walkthrough proves.**

1. A model is onboarded and live at v1.
2. The API is ready.
3. A real Alertmanager webhook is accepted.
4. A mapper turns it into exactly one job.
5. The pipeline chooses a strategy and registers a candidate while LIVE stays at v1.
6. The rollout enters canary at 5 %.
7. The serving side reports that same split.
8. Re-sending the alert returns the existing job instead of creating a new one.

**What the demo proves.** The demo has 15 stages:

| Stage | What is checked |
|---|---|
| 1 | onboarding |
| 2 | historical data |
| 3 | drifted data |
| 4 | drift detection (PSI, KS) |
| 5 | reuse analysis |
| 6 | strategy decision |
| 7 | adaptation |
| 8 | statistical validation |
| 9 | MLflow registration |
| 10 | promotion after a recorded approval |
| 11 | prediction with the promoted model |
| 12 | lineage |
| 13 | audit events |
| 14 | Prometheus metrics |
| 15 | duplicate-event handling |

[`docs/DEMO_MANUAL.md`](docs/DEMO_MANUAL.md) explains the output stage by stage. Each run writes to a new folder, `data/demo/run-<timestamp>/`, and deletes nothing. The last
line prints the `mlflow ui` command for that run.

On Windows the same checks are `.\scripts\run_local.ps1 demo | quicktest | test | lint | security`.

## A.5 Run the API

```bash
python -m uvicorn oran_adapt.api.main:app --host 127.0.0.1 --port 8000
```

| URL | Purpose |
|---|---|
| <http://127.0.0.1:8000/docs> | Interactive API (OpenAPI) |
| `/api/v1/health`, `/api/v1/readiness` | Liveness and readiness; no key needed |
| `/api/v1/metrics` | Prometheus metrics |

Submit a drift event:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/adaptation/events \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"model_id": "m1", "event_id": "evt-001", "drift_detected": true}'
```

## A.6 Use it on your own model and data

```bash
python -m oran_adapt.cli model onboard --model-id m1 --model-file model.joblib --framework sklearn \
    --task-type regressor --target throughput --dataset kpi --training-csv train.csv
python -m oran_adapt.cli data ingest --dataset kpi --version d1 --csv drifted.csv --kind DRIFTED
python -m oran_adapt.cli event submit --model-id m1 --dataset kpi --drifted-version d1
python -m oran_adapt.cli model show --model-id m1          # versions, live version, lineage
python -m oran_adapt.cli rollout list                      # the candidate's delivery state
python -m oran_adapt.cli rollout approve --rollout-id <id> # when DELIVERY_STRATEGY=manual
```

Supported model families: scikit-learn, XGBoost, LightGBM, CatBoost, PyTorch (tabular and
sequence models), Keras, statsmodels forecasters and ONNX. Any other family can use the
sandboxed LLM path, once `LLM_PROVIDER` is set.

## A.7 Container stack and Kubernetes (optional)

```bash
docker build -t oran-adapt-sandbox:latest -f docker/sandbox/Dockerfile docker/sandbox
docker compose up --build -d          # PostgreSQL, MLflow, Kafka + Debezium, API, Prometheus
curl http://127.0.0.1:8000/api/v1/readiness
docker compose down
```

Before starting the stack, set `POSTGRES_PASSWORD` and `API_KEYS` in `.env`. The ports are
bound to 127.0.0.1: API 8000, MLflow 5000, Kafka Connect 8083, Prometheus 9090.

```bash
helm lint deploy/helm/oran-adapt
helm install oran-adapt deploy/helm/oran-adapt -f deploy/helm/oran-adapt/examples/values-production.yaml
```

CI builds the images and runs the compose smoke test on every push. A weekly job
([`.github/workflows/k8s-e2e.yml`](.github/workflows/k8s-e2e.yml), with [`.github/workflows/ci.yml`](.github/workflows/ci.yml) for every push) installs, upgrades and rolls back the Helm chart on a
kind cluster.

## A.8 Phase gates

```bash
bash scripts/verify.sh 7         # one hardening phase: ruff → mypy → boundary test → no-gaps lint → scoped tests → acceptance
bash scripts/verify.sh all       # every phase gate plus audits
python scripts/lint_no_gaps.py   # no TODO, stub or bare except in shipped code
python scripts/audit.py          # re-runs the test behind every audit claim
```

---

# PART B — How the files link together

## B.1 Directory map

```
AGENTIC_AI_MODEL_ADAPTATION_FRAMEWORK_FOR_ORAN/
├── src/oran_adapt/            application code
│   ├── api/                   FastAPI app (app.py, main.py), 7 route modules, auth, rate limits
│   ├── orchestrator/          pipeline.py (Algorithm 1), jobs.py, worker.py, locks.py   [shared · Tamilselvan: jobs.py intake]
│   ├── analysis/              drift statistics, version scoring, reuse decision          [Tamilselvan · Nitesh · Ramanujam]
│   ├── decision/              constraints, LLM selector, fallback, decision report       [Ramanujam]
│   ├── adaptation/            inspector, capability, fine-tune / retrain / torch engines,
│   │                          leakage checks, llm_adapter                                 [shared]
│   ├── sandbox/               AST security scan and isolated runner for generated code    [shared]
│   ├── validation/            gate.py (paired bootstrap + guardrails), engine, metrics    [Bhuvaneshwar]
│   ├── registry/              onboarding, publishing, promotion, deployment read-back     [Bhuvaneshwar · Tamilselvan]
│   ├── delivery/              rollout controller: shadow, canary, A/B, manual, blue-green [Bhuvaneshwar]
│   ├── datastore/             content-hashed dataset versions, lineage, data by reference [Nitesh]
│   ├── notifications/         durable outbox, HMAC signing, dispatcher
│   ├── cdc/                   change-data capture from a source database
│   ├── llm/                   provider-neutral LLM client                                 [Ramanujam]
│   ├── ports/                 the interfaces (one per port)
│   ├── adapters/              the implementations: registries, serving, queues, auth, model types,
│   │                          sql and rankpi datasets, rankpi rollout metrics, ...
│   ├── conformance/           test suites every adapter of a port must pass
│   ├── core/                  config.py (every setting), enums, schemas, errors, policies
│   ├── db/                    SQLAlchemy models, session handling, migration runner       [Nitesh]
│   ├── bootstrap.py           composition root: reads config, resolves adapters, wires the app
│   ├── plugins.py             entry-point discovery for adapters
│   └── cli.py                 the oran-adapt command
├── tests/                     unit/, integration/, conftest.py, skip_policy.py (951 tests)
├── scripts/                   walkthrough.py, demo.py, demo_models.py, verify.sh, audits, CI helpers
├── config/                    examples/ (10 reference stacks), mappers/ (alert formats), policies/ (gate, delivery)
├── migrations/                Alembic migrations (expand-only)
├── deploy/                    compose, Helm, Kustomize, Debezium, Prometheus, Postgres init
├── docker/                    sandbox and MLflow images;  Dockerfile at the root builds api/worker/migrator
├── docs/                      MANUAL.md, DEMO_MANUAL.md, DEPLOYMENT.md, LIMITATIONS.md, ADRs, runbooks
├── pyproject.toml             dependencies, the oran-adapt script, adapter entry points
├── requirements.lock          pinned versions; install with -c requirements.lock
├── .env.example               every environment variable with a safe local default
├── RUN.md                     short copy-paste run guide
└── docker-compose.yml         the local container stack
```

**Quick access:**

| Area | Open |
|---|---|
| Application code | [`src/oran_adapt/`](src/oran_adapt/) — [`api`](src/oran_adapt/api/) · [`orchestrator`](src/oran_adapt/orchestrator/) · [`analysis`](src/oran_adapt/analysis/) · [`decision`](src/oran_adapt/decision/) · [`adaptation`](src/oran_adapt/adaptation/) · [`sandbox`](src/oran_adapt/sandbox/) · [`validation`](src/oran_adapt/validation/) · [`registry`](src/oran_adapt/registry/) · [`delivery`](src/oran_adapt/delivery/) · [`datastore`](src/oran_adapt/datastore/) · [`notifications`](src/oran_adapt/notifications/) · [`cdc`](src/oran_adapt/cdc/) · [`llm`](src/oran_adapt/llm/) · [`ports`](src/oran_adapt/ports/) · [`adapters`](src/oran_adapt/adapters/) · [`conformance`](src/oran_adapt/conformance/) · [`core`](src/oran_adapt/core/) · [`db`](src/oran_adapt/db/) |
| Entry points | [`bootstrap.py`](src/oran_adapt/bootstrap.py) · [`plugins.py`](src/oran_adapt/plugins.py) · [`cli.py`](src/oran_adapt/cli.py) · [`api/main.py`](src/oran_adapt/api/main.py) |
| Tests | [`tests/unit/`](tests/unit/) · [`tests/integration/`](tests/integration/) · [`conftest.py`](tests/conftest.py) |
| Scripts | [`walkthrough.py`](scripts/walkthrough.py) · [`demo.py`](scripts/demo.py) · [`demo_models.py`](scripts/demo_models.py) · [`verify.sh`](scripts/verify.sh) · [`audit.py`](scripts/audit.py) · [`lint_no_gaps.py`](scripts/lint_no_gaps.py) |
| Configuration | [`config/examples/`](config/examples/) · [`config/mappers/`](config/mappers/) · [`config/policies/`](config/policies/) · [`.env.example`](.env.example) |
| Packaging and deployment | [`pyproject.toml`](pyproject.toml) · [`requirements.lock`](requirements.lock) · [`Dockerfile`](Dockerfile) · [`docker-compose.yml`](docker-compose.yml) · [`docker/`](docker/) · [`deploy/`](deploy/) · [`migrations/`](migrations/) |
| Documentation | [`RUN.md`](RUN.md) · [`docs/MANUAL.md`](docs/MANUAL.md) · [`docs/`](docs/) |

## B.2 The dependency rule

```
api / cli / worker  ──►  orchestrator  ──►  analysis · decision · adaptation · validation · registry · delivery
                                                        │
                                                        ▼
                                                  ports/ (interfaces)
                                                        ▲
                                         adapters/ (implementations)  ◄── selected by config at start-up
```

The core packages never import a vendor SDK such as MLflow, boto3 or Kubernetes. They talk
only to the interfaces in [`ports/`](src/oran_adapt/ports/). At start-up, [`bootstrap.py`](src/oran_adapt/bootstrap.py) reads [`core/config.py`](src/oran_adapt/core/config.py) and
picks the adapter for each port from the entry points in [`pyproject.toml`](pyproject.toml).
[`tests/unit/test_import_boundary.py`](tests/unit/test_import_boundary.py) fails the build if the rule is broken. That is how one
pipeline runs unchanged against MLflow, a plain filesystem, SageMaker or Vertex.

## B.3 Paper architecture (Fig. 1) → code

| Paper block | Files | Function | Owner |
|---|---|---|---|
| Issue report in | [`api/routes_adaptation.py`](src/oran_adapt/api/routes_adaptation.py), [`config/mappers/`](config/mappers/) | `submit_event`, `submit_mapped_events` (Alertmanager, Evidently and generic-JSON mappers) | Tamilselvan R |
| 1. Model Intake | [`orchestrator/jobs.py`](src/oran_adapt/orchestrator/jobs.py), [`registry/onboarding.py`](src/oran_adapt/registry/onboarding.py) | idempotency by `event_id`, stale-version check; `onboard_model` | Tamilselvan R |
| 2. Independent Verification | [`analysis/engine.py`](src/oran_adapt/analysis/engine.py), [`analysis/comparison.py`](src/oran_adapt/analysis/comparison.py), [`analysis/reuse.py`](src/oran_adapt/analysis/reuse.py) | `analyze`: KS test and PSI per feature (Evidently AI); is the reported drift corroborated? | Tamilselvan R |
| 3. Data Retrieval & Analysis | [`analysis/retrieval.py`](src/oran_adapt/analysis/retrieval.py), [`analysis/merge.py`](src/oran_adapt/analysis/merge.py), [`datastore/access.py`](src/oran_adapt/datastore/access.py) | historical baseline and drifted window, by version or by reference | Tamilselvan R · Nitesh N D |
| Historical dataset registry (PostgreSQL) | [`datastore/versioning.py`](src/oran_adapt/datastore/versioning.py), [`db/models.py`](src/oran_adapt/db/models.py), [`migrations/`](migrations/) | SHA-256 content-hashed versions, parent lineage, model ↔ data links | Nitesh N D |
| 4. Decision Engine: reuse (Similar Model Exchange) | [`analysis/version_eval.py`](src/oran_adapt/analysis/version_eval.py), [`analysis/reuse_decision.py`](src/oran_adapt/analysis/reuse_decision.py) | `evaluate_versions`, `decide_reuse`: promote an earlier version that clearly beats LIVE | Nitesh N D |
| 4. Decision Engine: adapt | [`decision/engine.py`](src/oran_adapt/decision/engine.py), [`decision/constraints.py`](src/oran_adapt/decision/constraints.py), [`decision/llm_selector.py`](src/oran_adapt/decision/llm_selector.py), [`decision/fallback.py`](src/oran_adapt/decision/fallback.py), [`decision/report.py`](src/oran_adapt/decision/report.py) | `decide`: `FINE_TUNING`, `FULL_RETRAINING`, `ROLLBACK` or `NO_ACTION` | Ramanujam P |
| MLKTL Scenario Module (PSI severity) | [`analysis/summary.py`](src/oran_adapt/analysis/summary.py), [`decision/constraints.py`](src/oran_adapt/decision/constraints.py) | drift magnitude and severity; PSI ≥ 0.5 rules out fine-tuning | Ramanujam P |
| 5a / 5b. Fine-tune / Retrain | [`adaptation/finetune.py`](src/oran_adapt/adaptation/finetune.py), [`adaptation/retrain.py`](src/oran_adapt/adaptation/retrain.py), [`adaptation/torch_engine.py`](src/oran_adapt/adaptation/torch_engine.py), [`adaptation/model_types.py`](src/oran_adapt/adaptation/model_types.py) | the model type's engine for the chosen strategy (Generic Model Adapters) | shared |
| 5. Fallback for unknown models | [`adaptation/llm_adapter.py`](src/oran_adapt/adaptation/llm_adapter.py), [`sandbox/security.py`](src/oran_adapt/sandbox/security.py), [`sandbox/runner.py`](src/oran_adapt/sandbox/runner.py) | `adapt_via_llm`: generated code, AST-checked and run isolated | shared |
| 6. Validate | [`validation/engine.py`](src/oran_adapt/validation/engine.py), [`validation/gate.py`](src/oran_adapt/validation/gate.py) | `validate_candidate`; gate `decide`: deploy only if the 95 % paired-bootstrap interval lies above the margin, plus latency, size and calibration guardrails | Bhuvaneshwar T S |
| 7. Rollback | [`registry/promotion.py`](src/oran_adapt/registry/promotion.py), [`delivery/controller.py`](src/oran_adapt/delivery/controller.py) | the previous LIVE is kept on rejection; `POST /models/{id}/rollback` | Bhuvaneshwar T S |
| 8. Register | [`registry/publishing.py`](src/oran_adapt/registry/publishing.py), [`registry/promotion.py`](src/oran_adapt/registry/promotion.py), [`registry/deployment.py`](src/oran_adapt/registry/deployment.py) | `register_candidate`, `promote_version`, `start_rollout`, served-version read-back | Bhuvaneshwar T S |
| Experiments and evaluation | [`scripts/demo.py`](scripts/demo.py), [`scripts/demo_models.py`](scripts/demo_models.py), [`scripts/walkthrough.py`](scripts/walkthrough.py) | 15-stage demo, multi-model demo, clone-to-canary walkthrough | Bhuvaneshwar T S |
| MLflow registry | [`adapters/registry/mlflow/`](src/oran_adapt/adapters/registry/mlflow/) | versions, artefacts, the `live` alias, tags | — |

## B.4 One drift event through the code

| Step | File | What happens | Owner |
|---|---|---|---|
| 1 | [`api/routes_adaptation.py`](src/oran_adapt/api/routes_adaptation.py) | `POST /api/v1/adaptation/events` (or `/events/from/{mapper}`) | Tamilselvan R |
| 2 | [`orchestrator/jobs.py`](src/oran_adapt/orchestrator/jobs.py) | dedupe by `event_id`, create the AdaptationJob, enqueue it | Tamilselvan R |
| 3 | [`orchestrator/worker.py`](src/oran_adapt/orchestrator/worker.py) | lease the job (or run inline), enforce the deadline | shared |
| 4 | [`orchestrator/pipeline.py`](src/oran_adapt/orchestrator/pipeline.py) | Algorithm 1 — calls steps 5–14 in order | shared |
| 5 | [`analysis/engine.py`](src/oran_adapt/analysis/engine.py) | `analyze()`: retrieval, merge, KS + PSI, corroboration | Tamilselvan R |
| 6 | [`analysis/version_eval.py`](src/oran_adapt/analysis/version_eval.py) | `evaluate_versions()`: score every registered version | Nitesh N D |
| 7 | [`analysis/reuse_decision.py`](src/oran_adapt/analysis/reuse_decision.py) | `decide_reuse()`: Similar Model Exchange, or hand over | Nitesh N D |
| 8 | [`decision/engine.py`](src/oran_adapt/decision/engine.py) | `decide()`: constraints → LLM (validated) or fallback | Ramanujam P |
| 9 | [`adaptation/model_types.py`](src/oran_adapt/adaptation/model_types.py) | pick the plugin; fine-tune or retrain | shared |
| 10 | [`adaptation/llm_adapter.py`](src/oran_adapt/adaptation/llm_adapter.py) | `adapt_via_llm()`, only if no engine fits | shared |
| 11 | [`validation/engine.py`](src/oran_adapt/validation/engine.py) | `validate_candidate()` → paired-bootstrap gate | Bhuvaneshwar T S |
| 12 | [`registry/publishing.py`](src/oran_adapt/registry/publishing.py) | `register_candidate()`: MLflow version + lineage | Bhuvaneshwar T S |
| 13 | [`delivery/controller.py`](src/oran_adapt/delivery/controller.py) | `start_rollout()`: shadow / canary / A/B / manual | Bhuvaneshwar T S |
| 14 | [`registry/deployment.py`](src/oran_adapt/registry/deployment.py) | move traffic, read the served version back | Bhuvaneshwar T S |
| 15 | [`notifications/dispatcher.py`](src/oran_adapt/notifications/dispatcher.py) | signed event for every state change | shared |

[`scripts/walkthrough.py`](scripts/walkthrough.py) drives exactly this path and asserts each step. Reading the two
side by side is the quickest way into the code.

## B.5 Who reads and writes what

| Store | Written by | Read by |
|---|---|---|
| App database: SQLite by default, PostgreSQL in production ([`db/models.py`](src/oran_adapt/db/models.py)) | [`orchestrator/jobs.py`](src/oran_adapt/orchestrator/jobs.py), [`datastore/`](src/oran_adapt/datastore/), [`registry/promotion.py`](src/oran_adapt/registry/promotion.py), [`delivery/controller.py`](src/oran_adapt/delivery/controller.py), [`notifications/`](src/oran_adapt/notifications/) | every stage; [`api/`](src/oran_adapt/api/) for status |
| MLflow: model versions, artefacts, `live` alias, tags | [`registry/publishing.py`](src/oran_adapt/registry/publishing.py), [`registry/promotion.py`](src/oran_adapt/registry/promotion.py) | [`analysis/version_eval.py`](src/oran_adapt/analysis/version_eval.py), [`validation/`](src/oran_adapt/validation/), [`registry/deployment.py`](src/oran_adapt/registry/deployment.py) |
| Dataset versions (SHA-256 content hash, parent, role `TRAINING`, `VALIDATION` or `DRIFT_OBSERVED`) | [`datastore/versioning.py`](src/oran_adapt/datastore/versioning.py); `POST /datasets/{id}/versions` ([`api/routes_data.py`](src/oran_adapt/api/routes_data.py)); [`cdc/`](src/oran_adapt/cdc/) | [`analysis/retrieval.py`](src/oran_adapt/analysis/retrieval.py), [`adaptation/data.py`](src/oran_adapt/adaptation/data.py) |
| Audit trail and Prometheus metrics | every stage via [`orchestrator/context.py`](src/oran_adapt/orchestrator/context.py), [`core/metrics.py`](src/oran_adapt/core/metrics.py) | `GET /api/v1/metrics`, `GET /adaptation/jobs/{id}` |

## B.6 Configuration files and where they apply

| File | Used by | Purpose |
|---|---|---|
| `.env` (from [`.env.example`](.env.example)) | [`core/config.py`](src/oran_adapt/core/config.py) | every setting, for example `DATABASE_URL`, `MLFLOW_TRACKING_URI`, `LLM_PROVIDER`, `DELIVERY_STRATEGY`, `API_KEYS` |
| [`config/examples/*.toml`](config/examples/) | `ORAN_CONFIG_FILE` | ten reference stacks: development, production, airgapped, MLflow+KServe, SageMaker, Vertex, Seldon, Triton, BentoML, rankpi |
| [`config/policies/gate.toml`](config/policies/gate.toml) | [`validation/gate.py`](src/oran_adapt/validation/gate.py) | gate mode, margin, confidence, resamples, guardrails |
| [`config/policies/delivery.toml`](config/policies/delivery.toml), [`delivery-rankpi.toml`](config/policies/delivery-rankpi.toml) | [`delivery/controller.py`](src/oran_adapt/delivery/controller.py) | canary steps, hold times, health limits, approval TTL |
| [`config/mappers/*.toml`](config/mappers/) | `POST /events/from/{mapper}` | turn Alertmanager, Evidently or generic JSON alerts into drift events |

Key thresholds in [`core/config.py`](src/oran_adapt/core/config.py):

| Setting | Default | Meaning |
|---|---|---|
| `analysis_psi_reuse_threshold` | 0.1 | PSI below which the model may be reused |
| `analysis_ks_pvalue_reuse_threshold` | 0.05 | KS significance |
| `decision_full_retrain_psi_threshold` | 0.5 | PSI at or above which fine-tuning is ruled out |
| `decision_min_drifted_rows` | 10 | fewer drifted rows than this means no adaptation |
| `validation_holdout_fraction` | 0.2 | newest share of the data held out for the gate |
| `delivery_strategy` | `shadow` | how a validated candidate is delivered |

## B.7 Tests and what they cover

| Folder | Covers |
|---|---|
| [`tests/unit/`](tests/unit/) | each module, the gate, the state machine, every route, security, config lint, the import boundary, the rankpi integration ([`test_rankpi_integration.py`](tests/unit/test_rankpi_integration.py)) |
| [`tests/integration/`](tests/integration/) | real SQLite and MLflow end to end; PostgreSQL, Docker sandbox and compose (these skip themselves when the infrastructure is absent) |
| [`src/oran_adapt/conformance/`](src/oran_adapt/conformance/) | one suite per port; every adapter must pass its port's suite |

Markers: `smoke` is the fast tier, unmarked tests form the gate tier, and `heavy` (cluster,
large data, soak) is excluded by default via `addopts = -m "not heavy"`.

## B.8 Documentation map

| Read | For |
|---|---|
| [`RUN.md`](RUN.md) | the short copy-paste run guide |
| [`docs/MANUAL.md`](docs/MANUAL.md) | full operator reference: endpoints, settings, troubleshooting |
| [`docs/DEMO_MANUAL.md`](docs/DEMO_MANUAL.md) | the demo's output, stage by stage |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), [`docs/integration-guide.md`](docs/integration-guide.md) | compose, Helm, wiring a real stack |
| [`docs/integration/rankpi.md`](docs/integration/rankpi.md) | the OAI RAN `rankpi` database integration |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md), [`docs/PRODUCTION-READINESS.md`](docs/PRODUCTION-READINESS.md) | what is and is not verified |
| [`docs/adr/`](docs/adr/) | architecture decision records |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Sandbox tests fail with `exceeded the memory limit` | CUDA PyTorch installed | Reinstall with the CPU index (A.2) |
| A conformance test fails on Python 3.11 | `entry_points()` API difference | Use Python 3.12 or later |
| Two mypy errors in `notifications/service.py` | SQLAlchemy 2.1 | Install with `-c requirements.lock` |
| `401` / `403` from the API | No or wrong `X-API-Key` | A.3; health and readiness need no key |
| Job ends `DELIVERING` and LIVE does not change | Correct: the default `DELIVERY_STRATEGY=shadow` moves no traffic | Use `rollout approve` with `manual`, or a `canary` policy |
| Validation rejects a candidate that looks better | Its 95 % improvement interval spans zero | Send more drifted rows; covariate shift alone often needs no adaptation |
| Service exits naming a setting | Required setting missing (fail-fast) | Set the setting it names |

## Known limitations

- The framework **adapts** models; it does not detect drift. Events arrive over HTTP or
  through a mapper. There is no live RIC, xApp/rApp or A1/E2/O1 interface.
- The live `rankpi` database, cloud registries, serving systems and LLM providers have been
  tested against emulators and doubles. Their live tests skip without credentials.
- Docker and Kubernetes run in CI only; the development laptop has neither.
- The demo and walkthrough data are synthetic KPIs from fixed seeds, not an operator trace.

[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) has the full, current list.
