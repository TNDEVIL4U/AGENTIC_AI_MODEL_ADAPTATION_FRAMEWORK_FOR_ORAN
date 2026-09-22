# Phase 12: Demo Scripts, README, Docs

## What this phase adds

The final phase of the Phase 0 plan (`docs/PHASE0_AUDIT.md`, phase 12: "Demo scripts, README,
docs") closes out the project by making the whole pipeline demonstrable and documented without
requiring PostgreSQL, Docker, or an LLM API key:

1. `scripts/demo.py` — a self-contained, runnable walkthrough that migrates a local SQLite
   database, seeds a registered MLflow model plus historical/drifted data, then drives the real
   FastAPI app (the same ASGI app `uvicorn` serves) through `TestClient` across three scenarios:
   a readiness check, a no-drift event, a genuine-drift event, and a resubmission of the latter
   to exercise idempotency.
2. `README.md` — architecture diagram, package layout, setup instructions, environment variable
   reference table, API surface, testing instructions (including how the Postgres/Docker-gated
   integration tests opt themselves in), and an explicit "Honesty note" disclosing that the
   Docker backend has never been executed on this machine.
3. `docs/PHASE0_AUDIT.md` and `docs/PHASE11_DOCKER_E2E.md` — the audit/plan and the Docker
   sandbox status doc, both already in place and linked from the README's "Project history"
   section.

## Exit-gate status: verified, passing

Re-verified on this machine on 2026-09-22 after a prior session was interrupted (the local
machine restarted mid-session):

- `ruff check src tests` → all checks pass, no findings.
- `pytest` (full suite) → **150 passed, 6 skipped in 206s**. The 6 skips are all the
  documented, self-gating ones: the Docker-CLI-gated tests in
  `tests/integration/test_docker_sandbox.py` and `tests/integration/test_docker_compose_e2e.py`
  (no `docker` CLI on this machine), plus the POSIX-only `RLIMIT_AS` sandbox test (this machine
  is Windows). No unexpected failures or skips.
- `python scripts/demo.py` → ran to completion, exit code 0, against a fresh
  `data/demo/{app.db,mlflow.db}`. Output matched the README's description exactly:
  - `GET /api/v1/ready` → `200`, both `database` and `mlflow` components `ok: true`.
  - Scenario A (no drift) → `201`, `outcome: NO_ACTION`.
  - Scenario B (genuine drift) → `201`, `strategy: FULL_RETRAINING`, candidate validated
    (accuracy 1.0000 vs 1.0000, within 0.02 tolerance) → `outcome: REGISTERED`, MLflow model
    version 2 created, `live` alias moved to version 2.
  - Resubmitting scenario B's event → `200`, `duplicate: true`, same `job_id`.
- README's setup/env-var/API/testing sections were cross-checked against
  `src/oran_adapt/core/config.py`, `src/oran_adapt/api/routes_*.py`, and `pyproject.toml`'s
  `[tool.pytest.ini_options]` / `[tool.ruff]` — no drift found between the documentation and the
  code.

## What remains genuinely unverified

Unchanged from Phase 11: `docker build`, `docker run` (network/memory flags), and the full
`docker compose up` stack have never executed on this machine, since Docker is not installed
here. This is disclosed in the README's "Honesty note" and in `docs/PHASE11_DOCKER_E2E.md`, and
is not something this phase can close without a machine that has Docker available.

## Result

All 13 phases of the Phase 0 plan (0 through 12) are implemented, tested, linted, and — for
everything that doesn't require Docker or a real LLM API key — executed for real on this
machine. The project is buildable and the demo runs end to end.
