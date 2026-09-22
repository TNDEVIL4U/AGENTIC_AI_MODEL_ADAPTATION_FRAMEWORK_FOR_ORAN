"""Phase 7: LLM adapters, AST security, sandbox - malicious code is rejected before it ever runs,
and safe code executes for real in an isolated subprocess. No mocks for the sandbox itself (a
real subprocess, real joblib round trip); the LLM provider boundary uses a hand-written fake
implementing the same LlmClient protocol as test_phase3_decision.py, since it is genuinely
external and there is no local provider to call."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from oran_adapt.adaptation.llm_adapter import adapt_via_llm
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import LlmUnavailableError, SandboxExecutionError, UnsafeCodeError
from oran_adapt.sandbox.runner import run_in_sandbox
from oran_adapt.sandbox.security import check_code_safety

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"


def _frame(n: int = 50, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    prb = rng.uniform(0, 1, size=n)
    rsrp = rng.uniform(-120, -60, size=n)
    label = (prb > 0.5).astype(int)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp}), pd.Series(label, name=TARGET)


# ---- security.py: static AST analysis -----------------------------------------------------------
def test_check_code_safety_accepts_clean_code() -> None:
    code = (
        "from sklearn.linear_model import LogisticRegression\n"
        "def adapt(current_model, X, y):\n"
        "    model = LogisticRegression()\n"
        "    model.fit(X, y)\n"
        "    return model\n"
    )
    check_code_safety(code)  # must not raise


@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef adapt(current_model, X, y):\n    os.system('echo pwned')\n    return current_model\n",
        "import subprocess\ndef adapt(current_model, X, y):\n    subprocess.run(['ls'])\n    return current_model\n",
        "import socket\ndef adapt(current_model, X, y):\n    return current_model\n",
        "from os import path\ndef adapt(current_model, X, y):\n    return current_model\n",
    ],
    ids=["import-os", "import-subprocess", "import-socket", "from-os-import"],
)
def test_check_code_safety_rejects_disallowed_imports(code: str) -> None:
    with pytest.raises(UnsafeCodeError):
        check_code_safety(code)


@pytest.mark.parametrize(
    "code",
    [
        "def adapt(current_model, X, y):\n    return eval('current_model')\n",
        "def adapt(current_model, X, y):\n    exec('x = 1')\n    return current_model\n",
        "def adapt(current_model, X, y):\n    return __import__('os')\n",
        "def adapt(current_model, X, y):\n    f = open('/etc/passwd')\n    return current_model\n",
    ],
    ids=["eval", "exec", "dunder-import", "open"],
)
def test_check_code_safety_rejects_forbidden_builtins(code: str) -> None:
    with pytest.raises(UnsafeCodeError):
        check_code_safety(code)


def test_check_code_safety_rejects_dunder_attribute_escape() -> None:
    code = (
        "def adapt(current_model, X, y):\n"
        "    bases = current_model.__class__.__bases__\n"
        "    return current_model\n"
    )
    with pytest.raises(UnsafeCodeError):
        check_code_safety(code)


def test_check_code_safety_rejects_invalid_syntax() -> None:
    with pytest.raises(UnsafeCodeError):
        check_code_safety("def adapt(current_model, X, y:\n    return current_model\n")


def test_check_code_safety_reports_violations_in_error_context() -> None:
    with pytest.raises(UnsafeCodeError) as exc_info:
        check_code_safety("import os\ndef adapt(current_model, X, y):\n    return current_model\n")
    assert any("os" in v for v in exc_info.value.context["violations"])


# ---- runner.py: real subprocess execution --------------------------------------------------------
def test_run_in_sandbox_executes_safe_code_and_returns_model(tmp_path) -> None:
    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    code = (
        "from sklearn.linear_model import LogisticRegression\n"
        "def adapt(current_model, X, y):\n"
        "    model = LogisticRegression()\n"
        "    model.fit(X, y)\n"
        "    return model\n"
    )

    result = run_in_sandbox(
        code,
        current_model=current,
        X=X,
        y=y,
        timeout_s=30,
        memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
    )
    assert isinstance(result, LogisticRegression)
    preds = result.predict(X)
    assert len(preds) == len(X)


def test_run_in_sandbox_raises_on_runtime_error_in_code(tmp_path) -> None:
    X, y = _frame()
    code = "def adapt(current_model, X, y):\n    raise ValueError('boom')\n"

    with pytest.raises(SandboxExecutionError):
        run_in_sandbox(
            code,
            current_model=None,
            X=X,
            y=y,
            timeout_s=30,
            memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
        )


def test_run_in_sandbox_raises_on_timeout(tmp_path) -> None:
    code = "def adapt(current_model, X, y):\n    while True:\n        pass\n"
    X, y = _frame(n=5)

    with pytest.raises(SandboxExecutionError):
        run_in_sandbox(
            code,
            current_model=None,
            X=X,
            y=y,
            timeout_s=2,
            memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
        )


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_AS memory ceiling is POSIX-only")
def test_run_in_sandbox_enforces_memory_limit_on_posix(tmp_path) -> None:
    code = (
        "def adapt(current_model, X, y):\n"
        "    hog = bytearray(2 * 1024 * 1024 * 1024)\n"  # 2GB against a 64MB ceiling
        "    return current_model\n"
    )
    X, y = _frame(n=5)

    with pytest.raises(SandboxExecutionError):
        run_in_sandbox(
            code,
            current_model=None,
            X=X,
            y=y,
            timeout_s=30,
            memory_mb=64,
            workdir=str(tmp_path / "sandbox"),
        )


# ---- llm_adapter.py: LLM -> security check -> sandbox, end to end --------------------------------
class FakeLlmClient:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


_SAFE_CODE = (
    "from sklearn.linear_model import LogisticRegression\n"
    "def adapt(current_model, X, y):\n"
    "    model = LogisticRegression()\n"
    "    model.fit(X, y)\n"
    "    return model\n"
)

_MALICIOUS_CODE = (
    "import os\n"
    "def adapt(current_model, X, y):\n"
    "    os.system('rm -rf /')\n"
    "    return current_model\n"
)


def test_adapt_via_llm_happy_path_produces_candidate_model(tmp_path) -> None:
    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    client = FakeLlmClient(response=_SAFE_CODE)

    candidate = adapt_via_llm(
        client,
        current,
        framework="sklearn",
        model_class="LogisticRegression",
        X=X,
        y=y,
        target_column=TARGET,
        sandbox_timeout_s=30,
        sandbox_memory_mb=512,
        workdir=str(tmp_path / "sandbox"),
    )

    assert candidate.engine == EngineKind.LLM_GENERATED
    assert candidate.framework == "sklearn"
    assert candidate.n_train_rows == len(X)
    assert client.calls  # the prompt was actually sent
    import joblib

    reloaded = joblib.load(candidate.artifact_path)
    assert len(reloaded.predict(X)) == len(X)


def test_adapt_via_llm_strips_markdown_fences() -> None:
    from oran_adapt.adaptation.llm_adapter import _extract_code

    assert _extract_code(f"```python\n{_SAFE_CODE}```") == _SAFE_CODE.rstrip("\n")


def test_adapt_via_llm_rejects_malicious_code_before_sandbox_execution(tmp_path) -> None:
    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    client = FakeLlmClient(response=_MALICIOUS_CODE)
    workdir = tmp_path / "sandbox"

    with pytest.raises(UnsafeCodeError):
        adapt_via_llm(
            client,
            current,
            framework="sklearn",
            model_class="LogisticRegression",
            X=X,
            y=y,
            target_column=TARGET,
            sandbox_timeout_s=30,
            sandbox_memory_mb=512,
            workdir=str(workdir),
        )
    # The security check must reject the code before any subprocess/artifact work happens.
    assert not workdir.exists()


def test_adapt_via_llm_propagates_provider_failure(tmp_path) -> None:
    X, y = _frame()
    current = LogisticRegression().fit(X, y)
    client = FakeLlmClient(error=LlmUnavailableError("boom"))

    with pytest.raises(LlmUnavailableError):
        adapt_via_llm(
            client,
            current,
            framework="sklearn",
            model_class="LogisticRegression",
            X=X,
            y=y,
            target_column=TARGET,
            sandbox_timeout_s=30,
            sandbox_memory_mb=512,
            workdir=str(tmp_path / "sandbox"),
        )
