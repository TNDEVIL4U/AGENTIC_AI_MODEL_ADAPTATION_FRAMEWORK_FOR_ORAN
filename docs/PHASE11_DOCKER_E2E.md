# Phase 11: Docker E2E

## What this phase adds

Before this phase, `Settings.sandbox_backend` (`"docker"` vs `"subprocess"`) existed but was
dead configuration: `adaptation/llm_adapter.py` called `sandbox.runner.run_in_sandbox` (the
subprocess backend) unconditionally, regardless of the setting. This phase:

1. Implements the Docker sandbox backend itself: `run_in_docker()` in
   `src/oran_adapt/sandbox/runner.py`, with the same contract as `run_in_sandbox` (same script
   template, same input/output hand-off via `joblib` files in `workdir`), executed via
   `docker run --rm --network none --memory <n>m ...` against `docker/sandbox/Dockerfile`
   instead of a local Python subprocess.
2. Adds `run_sandboxed()`, a small dispatcher that picks `run_in_docker` or `run_in_sandbox` by
   a `backend` argument. This is the one function `adaptation/llm_adapter.py` now calls.
3. Wires `Settings.sandbox_backend` and `Settings.sandbox_docker_image` through
   `orchestrator/pipeline.py` -> `adapt_via_llm()` -> `run_sandboxed()`, so the setting is no
   longer dead: changing `SANDBOX_BACKEND=docker` in the environment actually switches which
   backend LLM-generated adaptation code runs in.
4. Adds `docker/sandbox/Dockerfile`, a minimal image (`numpy`, `pandas`, `scikit-learn`,
   `xgboost`, `torch`, `joblib` - exactly the packages `sandbox/security.py`'s AST scanner
   allows LLM-generated code to import) tagged to match `Settings.sandbox_docker_image`'s
   default (`oran-adapt-sandbox:latest`).
5. Adds two integration test files, both skipped module-wide when the `docker` CLI is not on
   `PATH`:
   - `tests/integration/test_docker_sandbox.py` - builds the sandbox image and runs
     `run_in_docker` for real: safe code returns a fitted model, a runtime error in the
     generated code raises `SandboxExecutionError`, an infinite loop times out into
     `SandboxExecutionError`, and a bogus `DOCKER_HOST` (simulating a stopped daemon) also
     raises `SandboxExecutionError` rather than a raw `OSError`/`CalledProcessError`.
   - `tests/integration/test_docker_compose_e2e.py` - the literal "Docker E2E" the phase is
     named for: `docker compose up --build` the existing `docker-compose.yml`
     (postgres + mlflow + api, all written in Phase 1), poll `/ready`, POST a real drift event to
     `/api/v1/adaptation/events`, assert a `JobResponse` comes back, POST it again and assert
     `duplicate: true`. Gated on Docker being present **and** an explicit
     `RUN_DOCKER_E2E=1` **and** `POSTGRES_PASSWORD` being set, since it is the only test in the
     suite that builds an image, pulls two more, and starts three containers - too expensive and
     side-effectful to run by default even where Docker exists.
6. Adds `tests/unit/test_phase11_docker_sandbox.py` - the dispatcher logic itself needs no
   Docker, so it is exercised for real (no mocks of the sandbox boundary; only
   `run_in_docker`/`run_in_sandbox` are monkeypatched to prove `run_sandboxed` and
   `adapt_via_llm` route to the correct one and forward the right arguments).

## Exit-gate status: written, not run

This machine has no `docker` CLI (`docker --version` -> command not found), so per the Phase 0
audit's own stated plan for this phase, the Docker backend and the compose E2E test are **written
in full but have never been executed here**. What was actually verified on this machine:

- `ruff check` passes on all new and modified files.
- The full test suite passes: 150 passed, 5 skipped (the 4 new `test_docker_sandbox.py`
  Docker-gated tests + `test_docker_compose_e2e.py`'s single test, all skipping cleanly for the
  documented reason; the pre-existing POSIX-only `RLIMIT_AS` test still skips on Windows as
  before).
- The 4 new `tests/unit/test_phase11_docker_sandbox.py` tests run for real and pass, proving the
  dispatch logic and the `Settings.sandbox_backend` -> `adapt_via_llm` -> `run_sandboxed` wiring
  is correct independent of whether Docker itself is available.
- No regression to any earlier phase: `pipeline.py`'s only change is two new keyword arguments
  passed through to `adapt_via_llm`, both with backward-compatible defaults
  (`sandbox_backend="subprocess"`), so every existing call site and test that predates this phase
  behaves exactly as before.

What remains genuinely unverified until Docker is available somewhere: whether `docker build`
actually succeeds against `docker/sandbox/Dockerfile`, whether the memory/network flags passed to
`docker run` behave as intended, and whether the full `docker-compose.yml` stack actually comes
up healthy and serves a real request end-to-end.

## Line-by-line file review (still no Docker CLI — static review only)

`docs/IMPLEMENTATION_CHECKLIST.md`'s "Known gaps" item 5 flagged that `Dockerfile`,
`docker/sandbox/Dockerfile`, and `docker-compose.yml` had been confirmed to exist but not read
line-by-line. Read in full and cross-checked against the code that depends on them:

- **`docker-compose.yml`** — parses as valid YAML (`yaml.safe_load`, verified this session).
  Three services: `postgres:16`, `ghcr.io/mlflow/mlflow:latest`, and `api` (built from the root
  `Dockerfile`). `api`'s `DATABASE_URL`/`MLFLOW_TRACKING_URI` correctly override `.env`'s local
  SQLite defaults with the in-compose Postgres/MLflow service hostnames, and `depends_on` waits
  for Postgres's `pg_isready` healthcheck before starting the API. **Not independently verified:**
  whether `ghcr.io/mlflow/mlflow:latest` actually resolves to a pullable image — this repo has no
  network-checked Docker registry access in this environment, so that image reference is taken on
  faith, not confirmed.
- **Root `Dockerfile`** — multi-stage-style single stage on `python:3.11-slim`; copies
  `pyproject.toml`/`alembic.ini`/`src`/`migrations`, `pip install`s the package itself (not
  `-e`, so this is a real, non-editable install inside the image — consistent with, and actually
  resolving, the "package not installed editable" gap noted for local dev in `docs/MANUAL.md`
  §11), creates a non-root `app` user, and its `CMD` runs `upgrade_to_head()` before starting
  `uvicorn`. This correctly matches `api`'s service definition in `docker-compose.yml` (same
  `DATABASE_URL` env var name the migration call reads via `get_settings()`). No mismatches found.
- **`docker/sandbox/Dockerfile`** — `WORKDIR /sandbox` matches the `-v <workdir>:/sandbox -w
  /sandbox` bind mount `run_in_docker()` constructs at `src/oran_adapt/sandbox/runner.py:181-184`
  exactly. The installed package list (`numpy, pandas, scikit-learn, xgboost, torch, joblib`)
  matches `sandbox/security.py`'s `ALLOWED_IMPORT_ROOTS` one-for-one — no import the AST scanner
  would permit is missing from the image, and no extra package (e.g. no shell utilities, no
  networking libraries) is present beyond what's needed. Tag convention in the file's own header
  comment (`oran-adapt-sandbox:latest`) matches `Settings.sandbox_docker_image`'s default in
  `src/oran_adapt/core/config.py:30`, confirmed by direct read.

**Conclusion: no defects found in this static review.** All three files are internally consistent
with each other and with the Python code that constructs `docker run`/reads these env vars. This
raises confidence but is **not a substitute for actually building and running them** — a `docker
build` failure (bad base image, a typo in a path) or a runtime failure (wrong permissions, a port
conflict) would not be caught by reading the files. That remains the one item in the "Known gaps"
list that genuinely requires a Docker install to close.
