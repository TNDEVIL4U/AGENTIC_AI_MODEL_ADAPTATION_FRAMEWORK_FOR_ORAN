# Full run commands (Windows PowerShell)

Every command to run the project, in order, from the repository root:

```powershell
cd "C:\AGENTIC AI MODEL ADAPTATION FRAMEWORK FOR ORAN ARCHITECTURE"
```

Run one step at a time (this laptop is 16 GB RAM, CPU only). Nothing needs a GPU, Docker,
PostgreSQL or an LLM key unless the step says so. `RUN.md` explains each step in more detail.

---

## Step 1: Setup (once)

```powershell
.\scripts\run_local.ps1 setup
```

This creates `.venv`, installs the project with its `dev` and `security` extras, and copies
`.env.example` to `.env` if `.env` does not exist yet. Needs Python 3.11 or newer.

Activate the venv in every new terminal before running the plain `python` commands below:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Step 2: Database migrations

```powershell
.\scripts\run_local.ps1 migrate
# same as: python -m oran_adapt.cli db upgrade
```

## Step 3: Make an API key (the API needs one for protected calls)

```powershell
.\scripts\run_local.ps1 newkey -Role ADMIN
# same as: python -m oran_adapt.cli auth new-key --role ADMIN
# other roles: OPERATOR, ML_ENGINEER, READ_ONLY
```

1. Copy the printed `api_keys_entry` into `API_KEYS` in `.env`, e.g. `API_KEYS={"<digest>": "ADMIN"}`.
2. Keep the printed `api_key` secret. It is shown once and stored nowhere. Send it as the
   `X-API-Key` header.

## Step 4: Check the code (optional, run one at a time)

```powershell
.\scripts\run_local.ps1 lint        # ruff, a few seconds
.\scripts\run_local.ps1 quicktest   # fast unit tests, about 1-2 min
.\scripts\run_local.ps1 test        # full suite, about 15-20 min (heavy)
.\scripts\run_local.ps1 security    # bandit, pip-audit, mypy, SBOM into .\reports, 1-3 min
python scripts/security_check.py --skip-audit   # security checks when offline
```

## Step 5: Run the end-to-end demo (recommended first run)

```powershell
.\scripts\run_local.ps1 demo
# same as: python scripts/demo.py
```

Self-contained, about 1 minute. It checks 15 stages (onboarding, drift detection, analysis,
decision, adaptation, validation, MLflow registration, promotion, prediction, lineage, audit,
metrics, duplicate events). Output goes to a new `data\demo\run-<timestamp>\` folder. On failure
it prints `DEMO FAILED: ...` and exits with code 1. The last line prints an `mlflow ui` command
for that run.

Multi-model demo (sklearn, xgboost and torch models taking different adaptation paths):

```powershell
python scripts/demo_models.py
python scripts/demo_models.py --start-server   # also starts a local MLflow server for the run
```

## Step 6: Run the API

```powershell
.\scripts\run_local.ps1 api
# same as: python -m uvicorn oran_adapt.api.main:app --host 127.0.0.1 --port 8000
```

Open in a browser:

- http://127.0.0.1:8000/docs (interactive API)
- http://127.0.0.1:8000/api/v1/health and http://127.0.0.1:8000/api/v1/readiness (no key needed)
- http://127.0.0.1:8000/api/v1/metrics (Prometheus metrics)

Call a protected endpoint from a second terminal:

```powershell
$h = @{ "X-API-Key" = "<your api_key from step 3>" }
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v1/models     # list models
Invoke-RestMethod -Headers $h http://127.0.0.1:8000/api/v1/datasets   # list datasets
```

Stop the API with **Ctrl+C**.

## Step 7: Optional local MLflow server

```powershell
python scripts/run_mlflow_server.py              # http://127.0.0.1:5000, Ctrl+C stops it
python scripts/run_mlflow_server.py --port 5001
```

To use it, set `MLFLOW_TRACKING_URI` and `MLFLOW_REGISTRY_URI` to `http://127.0.0.1:5000` in `.env`.
Do not run it at the same time as the full test suite.

## Step 8: CLI workflow with your own model and data

```powershell
# register a trusted local model with its training data
python -m oran_adapt.cli model onboard --model-id m1 --model-file model.joblib --framework sklearn `
    --task-type regressor --target throughput --dataset kpi --training-csv train.csv

# store drifted data as a new immutable version
python -m oran_adapt.cli data ingest --dataset kpi --version d1 --csv drifted.csv --kind DRIFTED

# run the adaptation pipeline for that drift
python -m oran_adapt.cli event submit --model-id m1 --dataset kpi --drifted-version d1

# inspect results
python -m oran_adapt.cli model show --model-id m1
python -m oran_adapt.cli data current --model-id m1
python -m oran_adapt.cli data list --dataset kpi
python -m oran_adapt.cli data lineage --dataset kpi --version d1
```

Other CLI commands:

```powershell
python -m oran_adapt.cli --help                        # every command
python -m oran_adapt.cli data create-dataset --dataset kpi --name "KPI data"
python -m oran_adapt.cli model attach --model-id m2 --mlflow-name <registered-name> `
    --framework sklearn --task-type classifier --target label
python -m oran_adapt.cli cdc run --mode polling --once   # consume one batch of CDC events
python -m oran_adapt.cli cdc materialize --dataset kpi   # fold CDC events into a new version
```

`--framework` accepts `sklearn`, `xgboost` or `torch`. `--task-type` accepts `classifier` or
`regressor`. `docs\CDC.md` covers CDC in detail.

## Step 9: Full Docker stack (only on a machine with Docker)

Not runnable on this laptop, because Docker is not installed. First set `POSTGRES_PASSWORD`,
`MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` and `API_KEYS` in `.env`.

```powershell
.\scripts\run_local.ps1 docker        # builds the sandbox image, then: docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness
$env:RUN_DOCKER_E2E = "1"
python -m pytest -q tests/integration/test_docker_compose_e2e.py tests/integration/test_docker_sandbox.py
.\scripts\run_local.ps1 docker-down   # stop the stack (data volumes are kept)
```

Ports (all bound to 127.0.0.1): API 8000, MLflow 5000, MinIO console 9001, Kafka Connect 8083,
Prometheus 9090.

---

## Shortest path to see it working

```powershell
cd "C:\AGENTIC AI MODEL ADAPTATION FRAMEWORK FOR ORAN ARCHITECTURE"
.\scripts\run_local.ps1 setup
.\scripts\run_local.ps1 migrate
.\scripts\run_local.ps1 demo
.\scripts\run_local.ps1 newkey -Role ADMIN   # then paste api_keys_entry into API_KEYS in .env
.\scripts\run_local.ps1 api                  # open http://127.0.0.1:8000/docs, Ctrl+C to stop
```

If PowerShell blocks the script ("running scripts is disabled"), run it for this one call only:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_local.ps1 demo
```
