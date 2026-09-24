# Deployment

> **Status:** the Docker images and `docker-compose.yml` were written and statically checked
> (the YAML parses, and paths and settings match the code), but they have **not been built or
> run**. None of the image tags below has been pulled. Treat the first `docker compose up` as
> a test run.

## Services (`docker-compose.yml`)

| Service | Image | Role | Published port |
|---------|-------|------|----------------|
| `postgres` | `postgres:16.4-bookworm` | App database `oran_adapt` and MLflow database `mlflow` (`deploy/postgres/init-mlflow.sql`). Runs with `wal_level=logical` for Debezium. | none |
| `minio` | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | S3 store for MLflow model artifacts | 127.0.0.1:9001 (console) |
| `minio-init` | `minio/mc:RELEASE.2024-11-21T17-21-54Z` | Creates the `mlflow` bucket, then exits | none |
| `mlflow` | built from `docker/mlflow/` | Tracking and registry server. Metadata goes to PostgreSQL; artifacts go to `s3://mlflow`, proxied with `--serve-artifacts`, so the API needs no S3 credentials | 127.0.0.1:5000 |
| `kafka` | `apache/kafka:3.8.0` | Single-node broker (KRaft) | none |
| `debezium` | `quay.io/debezium/connect:2.7.3.Final` | Kafka Connect with the PostgreSQL connector | 127.0.0.1:8083 |
| `debezium-init` | `curlimages/curl:8.10.1` | Registers `deploy/debezium/kpi-connector.json` once the API is healthy | none |
| `api` | built from `Dockerfile` | FastAPI app. Applies the migrations on start | 127.0.0.1:8000 |
| `cdc-consumer` | same image as `api` | `oran-adapt cdc run --mode kafka` (see [CDC.md](CDC.md)) | none |
| `prometheus` | `prom/prometheus:v2.54.1` | Scrapes `api:8000/api/v1/metrics` (`deploy/prometheus/prometheus.yml`) | 127.0.0.1:9090 |

Named volumes: `pgdata`, `miniodata`, `kafkadata`, `promdata`. Every published port binds to
`127.0.0.1` only. To expose the stack, put a TLS-terminating reverse proxy in front of it.

## Required `.env`

Compose reads `.env` both for variable substitution and as the `env_file` of the app services.
It refuses to start without the required variables.

| Variable | Required | Notes |
|----------|----------|-------|
| `POSTGRES_PASSWORD` | yes | Used by the app, MLflow and Debezium (Debezium reads it as `${env:...}`, so it never appears in the connector config) |
| `MINIO_ROOT_PASSWORD` | yes | At least 8 characters |
| `MINIO_ROOT_USER` | no | Default `oran` |
| `API_KEYS` | yes, in practice | JSON `{"<sha256 of key>": "ROLE[:name]"}`. With auth on and no keys, every protected endpoint refuses (fail closed). Make entries with `oran-adapt auth new-key --role OPERATOR --name <who>`. Roles: `ADMIN`, `OPERATOR`, `ML_ENGINEER`, `READ_ONLY` |
| `LLM_PROVIDER` and `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` | depends | Only when an LLM provider is enabled |

Compose itself sets `DATABASE_URL`, `MLFLOW_TRACKING_URI` and `KAFKA_BOOTSTRAP_SERVERS` for the
app services. Never commit `.env`: it is in `.gitignore` and `.dockerignore`.

```
docker compose build
docker compose up -d
curl http://127.0.0.1:8000/api/v1/ready
```

## Image security posture

- **App image** (`Dockerfile`): multi-stage build. The runtime image has no compilers. It runs
  as the non-root user `app` (uid 10001). The code is owned by root and is read-only for the
  app user, and only `/tmp` is writable (`ARTIFACT_WORKDIR=/tmp/oran-adapt/artifacts`). It
  installs the CPU-only torch wheel and includes a healthcheck on `/api/v1/health`.
- **MLflow image:** non-root user `mlflow` (uid 10001), with pinned `mlflow` and `psycopg`.
- **Sandbox image** (`docker/sandbox/Dockerfile`): pinned packages, but it **still runs as
  root**. This is deliberate for now, because the sandbox writes into a bind-mounted host
  directory and a non-root uid could fail on the host's permissions. Revisit this when the
  sandbox runs in the deployment.
- pip is upgraded to `>=26.2` in every image, because pip 25.x has open advisories.
- `.dockerignore` keeps `.env*`, `.git`, virtualenvs, local data, `mlruns` and tests out of the
  build context.

## Pinned dependencies (`requirements.lock`)

The lock holds 154 exact pins for the runtime dependencies. The Dockerfile applies it as a
constraints file (`pip install -c requirements.lock ...`), so each locked package is installed
at exactly its locked version, while Linux-only dependencies that a Windows-generated lock cannot
contain are still resolved. To regenerate it after changing the dependencies in
`pyproject.toml`, install them in the virtualenv first, then run:

```
python scripts/lock_requirements.py                 # writes requirements.lock
python scripts/lock_requirements.py --extra kafka   # also pins confluent-kafka (if installed)
```

`confluent-kafka` is not installed in the development virtualenv, so it is not pinned. In the
image it is resolved from the `kafka` extra (`>=2.3`).

## Security checks (`scripts/security_check.py`)

```
pip install -e ".[security]"
python scripts/security_check.py            # --skip-audit when offline
```

These run one after another and write their output to `reports/` (ignored by git):

| Check | Gate | Result on 2026-09-24 |
|-------|------|----------------------|
| bandit (`src/`) | no MEDIUM/HIGH | 0 high, 0 medium, 3 low (subprocess use in the sandbox runner: list arguments, no shell). B101 `assert` is skipped with a reason in `pyproject.toml` |
| pip-audit (`requirements.lock`) | no advisory unless accepted | 0 open advisories. 1 accepted: `PYSEC-2026-3740` in nltk (a dependency of evidently; oran-adapt never uses nltk; no fixed version exists) |
| mypy (`src/`) | error count must not rise above the baseline | 50 errors = baseline 50 (pre-existing, mostly Optional narrowing). Lower `MYPY_BASELINE` as they are fixed |
| SBOM | must generate | CycloneDX JSON, 154 components, `reports/sbom.cdx.json` |

The local development virtualenv's pip was upgraded from 25.2, which pip-audit flags, to 26.2.1
(`python -m pip install --upgrade "pip>=26.2"` inside `.venv`).

## Not yet verified

- `docker compose build` and `up`: none of the images has been built, and the tags have not
  been pulled.
- The end-to-end test `tests/integration/test_docker_compose_e2e.py`. It is skipped unless
  Docker, `MINIO_ROOT_PASSWORD` and `E2E_API_KEY` are available.
- The Debezium → Kafka → consumer path, and connector registration.
- MLflow with the PostgreSQL backend and S3 (MinIO) artifacts.
