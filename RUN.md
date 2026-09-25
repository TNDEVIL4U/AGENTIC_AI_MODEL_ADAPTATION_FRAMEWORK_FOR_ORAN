# How to run the project

The short, copy-paste guide. Background, architecture and configuration details are in
`README.md`, `docs/MANUAL.md` and `docs/DEPLOYMENT.md`.

All commands run from the repository root. On Windows, `scripts\run_local.ps1` wraps each step;
the plain commands it runs are shown next to it for macOS/Linux or for running by hand.
Each step runs one process at a time; nothing needs a GPU.

## 1. Setup (once)

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 setup` | `python -m venv .venv` then `.venv/bin/python -m pip install -e ".[dev,security]"` and `cp .env.example .env` |

Needs Python 3.11 or newer (developed on 3.13). Edit `.env` afterwards; every variable has a
safe local default except the ones marked in `README.md` (LLM keys, `POSTGRES_PASSWORD`,
`API_KEYS`). Without changes the project uses SQLite files under `data/` and no LLM.

## 2. Database migrations

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 migrate` | `python -m oran_adapt.cli db upgrade` |

## 3. API key (the API refuses every protected call without one)

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 newkey -Role ADMIN` | `python -m oran_adapt.cli auth new-key --role ADMIN` |

Copy the printed `api_keys_entry` into `API_KEYS` in `.env`, for example
`API_KEYS={"<digest>": "ADMIN"}`. Send the printed `api_key` as the `X-API-Key` header. It is
shown once and stored nowhere.

## 4. Tests

| What | Windows | By hand | Time |
|---|---|---|---|
| Fast unit tests | `.\scripts\run_local.ps1 quicktest` | see the ignore list in `scripts/run_local.ps1` | 1-2 min |
| Full suite | `.\scripts\run_local.ps1 test` | `python -m pytest -q tests` | 15-20 min |
| Lint | `.\scripts\run_local.ps1 lint` | `python -m ruff check src tests` | seconds |
| Security checks | `.\scripts\run_local.ps1 security` | `python scripts/security_check.py` | 1-3 min |

The full suite runs real adaptation jobs, each in its own worker process (about 18 s to start
each on a small laptop), which is where most of its time goes. Tests that need PostgreSQL or
Docker skip themselves when those are not available (see `README.md`, "Testing").
`security_check.py` writes bandit, pip-audit, mypy and SBOM output to `reports/`; pass
`--skip-audit` when offline.

## 5. Run the API

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 api` | `python -m uvicorn oran_adapt.api.main:app --host 127.0.0.1 --port 8000` |

Then:

- `http://127.0.0.1:8000/docs` for the interactive API
- `http://127.0.0.1:8000/api/v1/health` and `/api/v1/readiness` (no key needed)
- `http://127.0.0.1:8000/api/v1/metrics` (Prometheus; public unless `METRICS_PUBLIC=false`)

Stop with Ctrl+C.

## 6. Demo

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 demo` | `python scripts/demo.py` |

This is self-contained: no Docker, PostgreSQL or LLM key, about 1 minute. It drives the real
API against real SQLite and MLflow stores and checks 15 stages: onboarding, historical data,
drifted data, drift detection, analysis, strategy decision, adaptation, validation, MLflow
registration, promotion, prediction with the promoted model, lineage, audit events, metrics and
duplicate-event handling. Any stage that does not do what it should stops the demo with
`DEMO FAILED: ...` and exit code 1. Each run writes to a new `data/demo/run-<timestamp>/`
folder and deletes nothing; the last line prints the `mlflow ui` command for that run. The data
is synthetic KPI data from fixed seeds, not real O-RAN data. `docs/DEMO_MANUAL.md` walks
through the output. `python scripts/demo_models.py` runs the multi-model demo.

## 7. Command-line workflow (your own model and data)

```text
python -m oran_adapt.cli model onboard --model-id m1 --model-file model.joblib --framework sklearn \
    --task-type regressor --target throughput --dataset kpi --training-csv train.csv
python -m oran_adapt.cli data ingest --dataset kpi --version d1 --csv drifted.csv --kind DRIFTED
python -m oran_adapt.cli event submit --model-id m1 --dataset kpi --drifted-version d1
python -m oran_adapt.cli model show --model-id m1
python -m oran_adapt.cli data current --model-id m1
```

`python -m oran_adapt.cli --help` lists every command. `docs/CDC.md` covers
`cdc run` / `cdc materialize`.

## 8. Docker stack (PostgreSQL, MinIO, MLflow, Kafka + Debezium, API, Prometheus)

| Windows | By hand |
|---|---|
| `.\scripts\run_local.ps1 docker` | `docker build -t oran-adapt-sandbox:latest -f docker/sandbox/Dockerfile docker/sandbox` then `docker compose up --build -d` |
| `.\scripts\run_local.ps1 docker-down` | `docker compose down` |

Set `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` and `API_KEYS` in `.env`
first. Ports are bound to 127.0.0.1 only: API 8000, MLflow 5000, MinIO console 9001,
Kafka Connect 8083, Prometheus 9090.

Verify the stack:

```text
docker compose ps
curl http://127.0.0.1:8000/api/v1/readiness
set RUN_DOCKER_E2E=1   (bash: export RUN_DOCKER_E2E=1)
python -m pytest -q tests/integration/test_docker_compose_e2e.py tests/integration/test_docker_sandbox.py
```

**Status: BLOCKED — RESOURCE REQUIRED.** Docker is not installed on the development machine,
so the images, the compose stack and the Docker integration tests have never run there. The
commands above are how to verify them on a machine with Docker. `docs/DEPLOYMENT.md` has the
details.
