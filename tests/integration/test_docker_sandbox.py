"""Phase 11 (Docker E2E): exercises `sandbox.runner.run_in_docker` against a real Docker daemon,
the same way tests/unit/test_phase7_sandbox.py exercises the subprocess backend. Skips itself
(module-wide) when the `docker` CLI is not on PATH - true on the machine this was written on, so
this file has never actually run; it exists so the Docker backend is genuinely testable wherever
Docker is available, per the Phase 0 audit's "written in full, not run here" plan for Phase 11."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from sklearn.linear_model import LogisticRegression
from tests.unit.test_phase7_sandbox import _SAFE_CODE, _frame

from oran_adapt.core.errors import SandboxExecutionError
from oran_adapt.sandbox.runner import run_in_docker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI is not installed on this host"),
]

_IMAGE = "oran-adapt-sandbox:test"
_DOCKERFILE_DIR = "docker/sandbox"


@pytest.fixture(scope="module")
def sandbox_image() -> str:
    subprocess.run(
        ["docker", "build", "-t", _IMAGE, "-f", f"{_DOCKERFILE_DIR}/Dockerfile", _DOCKERFILE_DIR],
        check=True,
        timeout=900,
    )
    return _IMAGE


def test_run_in_docker_executes_safe_code_and_returns_model(sandbox_image, tmp_path) -> None:
    X, y = _frame()
    current = LogisticRegression().fit(X, y)

    result = run_in_docker(
        _SAFE_CODE,
        current_model=current,
        X=X,
        y=y,
        timeout_s=60,
        memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
        image=sandbox_image,
    )
    assert isinstance(result, LogisticRegression)
    assert len(result.predict(X)) == len(X)


def test_run_in_docker_raises_on_runtime_error_in_code(sandbox_image, tmp_path) -> None:
    X, y = _frame()
    code = "def adapt(current_model, X, y):\n    raise ValueError('boom')\n"

    with pytest.raises(SandboxExecutionError):
        run_in_docker(
            code,
            current_model=None,
            X=X,
            y=y,
            timeout_s=60,
            memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
            image=sandbox_image,
        )


def test_run_in_docker_raises_on_timeout(sandbox_image, tmp_path) -> None:
    code = "def adapt(current_model, X, y):\n    while True:\n        pass\n"
    X, y = _frame(n=5)

    with pytest.raises(SandboxExecutionError):
        run_in_docker(
            code,
            current_model=None,
            X=X,
            y=y,
            timeout_s=5,
            memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
            image=sandbox_image,
        )


def test_run_in_docker_raises_clean_error_when_docker_unavailable(tmp_path, monkeypatch) -> None:
    """Even on a host with the CLI installed, a daemon that isn't running must surface as
    SandboxExecutionError, not a raw subprocess/OSError - simulated by pointing at a bogus
    DOCKER_HOST rather than actually stopping the daemon."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")
    X, y = _frame(n=5)

    with pytest.raises(SandboxExecutionError):
        run_in_docker(
            _SAFE_CODE,
            current_model=None,
            X=X,
            y=y,
            timeout_s=10,
            memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
            image=_IMAGE,
        )
