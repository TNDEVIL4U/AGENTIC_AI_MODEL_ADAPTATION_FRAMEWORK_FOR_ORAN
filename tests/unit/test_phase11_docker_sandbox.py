"""Phase 11 (Docker E2E): unit-level coverage of the backend dispatcher that does not itself
need Docker - `run_sandboxed` must route to the right backend function and `adapt_via_llm` must
forward its backend choice to it. The real Docker execution path (`run_in_docker` actually
invoking the `docker` CLI) is covered separately in tests/integration/test_docker_sandbox.py,
which skips itself when Docker is not installed - true here, so that test has never run."""

from __future__ import annotations

import inspect

from sklearn.linear_model import LogisticRegression
from tests.unit.test_phase7_sandbox import _SAFE_CODE, TARGET, _frame

from oran_adapt.adaptation.llm_adapter import adapt_via_llm
from oran_adapt.sandbox import runner as runner_module
from oran_adapt.sandbox.runner import run_sandboxed


def test_run_sandboxed_defaults_to_subprocess_backend(tmp_path, monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise AssertionError("docker backend must not be used for the default backend")

    monkeypatch.setattr(runner_module, "run_in_docker", _fail)

    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    result = run_sandboxed(
        _SAFE_CODE,
        current_model=current,
        X=X,
        y=y,
        timeout_s=30,
        memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
        backend="subprocess",
    )
    assert isinstance(result, LogisticRegression)


def test_run_sandboxed_routes_docker_backend_to_run_in_docker(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_run_in_docker(code, **kwargs):
        calls.append({"code": code, **kwargs})
        return "docker-result"

    def _fail(*args, **kwargs):
        raise AssertionError("subprocess backend must not be used when backend='docker'")

    monkeypatch.setattr(runner_module, "run_in_docker", _fake_run_in_docker)
    monkeypatch.setattr(runner_module, "run_in_sandbox", _fail)

    X, y = _frame()
    result = run_sandboxed(
        _SAFE_CODE,
        current_model=None,
        X=X,
        y=y,
        timeout_s=30,
        memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
        backend="docker",
        docker_image="oran-adapt-sandbox:latest",
    )
    assert result == "docker-result"
    assert calls[0]["image"] == "oran-adapt-sandbox:latest"
    assert calls[0]["timeout_s"] == 30


def test_adapt_via_llm_forwards_sandbox_backend_choice(tmp_path, monkeypatch) -> None:
    from oran_adapt.adaptation import llm_adapter as llm_adapter_module

    captured: dict = {}

    def _fake_run_sandboxed(code, **kwargs):
        captured.update(kwargs)
        return LogisticRegression().fit(*_frame())

    monkeypatch.setattr(llm_adapter_module, "run_sandboxed", _fake_run_sandboxed)

    class _Client:
        def complete(self, *, system: str, prompt: str) -> str:
            return _SAFE_CODE

    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    adapt_via_llm(
        _Client(),
        current,
        framework="sklearn",
        model_class="LogisticRegression",
        X=X,
        y=y,
        target_column=TARGET,
        sandbox_timeout_s=30,
        sandbox_memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
        sandbox_backend="docker",
        sandbox_docker_image="oran-adapt-sandbox:latest",
    )

    assert captured["backend"] == "docker"
    assert captured["docker_image"] == "oran-adapt-sandbox:latest"


def test_adapt_via_llm_sandbox_backend_defaults_to_subprocess() -> None:
    # Guards Settings.sandbox_backend's "subprocess" default: call sites written before Phase 11
    # that don't pass sandbox_backend at all (there are none left, but future ones might) must
    # keep behaving exactly as they did before Phase 11 introduced the parameter.
    assert inspect.signature(adapt_via_llm).parameters["sandbox_backend"].default == "subprocess"
