"""Phase 11 (Docker E2E): the end-to-end scenario the phase is named for - brings up the real
docker-compose stack (PostgreSQL + MLflow + the API, all built from the repo's own Dockerfile
and docker-compose.yml) and drives it over real HTTP, instead of the in-process TestClient every
other test in this suite uses.

This is deliberately the most expensive and most side-effectful test here: it builds an image,
pulls two more, starts three containers, and tears them down again. So on top of the module-wide
Docker-availability skip every Phase 11 integration test uses, this one also requires an explicit
RUN_DOCKER_E2E=1 opt-in and a POSTGRES_PASSWORD in the environment (docker-compose.yml requires
that variable to start postgres at all). Neither is set on the machine this was written on, and
no `docker` CLI is installed there either, so this test has never actually been executed - it is
written in full, per the Phase 0 audit's stated plan for Phase 11, for wherever Docker is."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import httpx
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("docker") is None
        or os.environ.get("RUN_DOCKER_E2E") != "1"
        or not os.environ.get("POSTGRES_PASSWORD"),
        reason=(
            "requires the docker CLI, RUN_DOCKER_E2E=1, and POSTGRES_PASSWORD set - this brings "
            "up real containers and is not run by default"
        ),
    ),
]

_BASE_URL = "http://localhost:8000"


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=REPO_ROOT, check=True, timeout=900)


def test_docker_compose_stack_serves_a_real_adaptation_request() -> None:
    _compose("up", "-d", "--build")
    try:
        deadline = time.monotonic() + 180
        ready = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{_BASE_URL}/ready", timeout=5).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(3)
        if not ready:
            pytest.fail("api + postgres + mlflow never became ready within 180s")

        event = {
            "model_id": "no-such-model-in-registry",
            "drift_detected": True,
            "drift_score": 0.9,
        }
        resp = httpx.post(f"{_BASE_URL}/api/v1/adaptation/events", json=event, timeout=30)
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["model_id"] == "no-such-model-in-registry"
        assert body["status"] in ("FAILED", "COMPLETED")

        dup = httpx.post(f"{_BASE_URL}/api/v1/adaptation/events", json=event, timeout=30)
        assert dup.status_code == 200
        assert dup.json()["duplicate"] is True
    finally:
        _compose("down", "-v")
