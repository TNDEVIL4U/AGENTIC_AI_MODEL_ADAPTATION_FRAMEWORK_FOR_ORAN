"""The write -> check -> run -> fix loop of the LLM adapter (adaptation.llm_adapter): code the
security scan rejects, or that fails in the sandbox, goes back to the LLM with the error, and the
loop stops at ADAPTATION_AGENT_MAX_ATTEMPTS. The security scan is the real one; the sandbox run is
scripted (tests/unit/test_phase7_sandbox.py runs the real sandbox). Nothing reaches a real LLM."""

from __future__ import annotations

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from oran_adapt.adaptation import llm_adapter
from oran_adapt.adaptation.llm_adapter import adapt_via_llm
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import LlmCircuitOpenError, SandboxExecutionError, UnsafeCodeError
from oran_adapt.llm import calls

pytestmark = pytest.mark.smoke

GOOD = (
    "from sklearn.linear_model import LogisticRegression\n"
    "def adapt(current_model, X, y):\n"
    "    return LogisticRegression().fit(X, y)\n"
)
UNSAFE = "import os\ndef adapt(current_model, X, y):\n    os.system('id')\n    return current_model\n"
NOT_PYTHON = "def adapt(current_model, X, y)\n    return current_model\n"
CRASHES = "def adapt(current_model, X, y):\n    return current_model.fit(X, y[:3])  # CRASH\n"


class ScriptedLlm:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def data() -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"prb_util": rng.uniform(0, 1, 40), "rsrp": rng.uniform(-120, -60, 40)})
    return X, pd.Series((X["prb_util"] > 0.5).astype(int), name="label")


@pytest.fixture
def sandbox(monkeypatch) -> list[str]:
    """The sandbox run, scripted: code marked CRASH fails with a traceback, the rest fits."""
    ran: list[str] = []

    def run(code, *, X, y, workdir, **_):
        ran.append(workdir)
        if "CRASH" in code:
            raise SandboxExecutionError(
                "sandbox script exited with a non-zero status", returncode=1,
                stderr="Traceback ...\nValueError: Found input variables with inconsistent "
                       "numbers of samples: [40, 3]",
            )
        return LogisticRegression().fit(X, y)

    monkeypatch.setattr(llm_adapter, "run_sandboxed", run)
    return ran


def _adapt(client, data, tmp_path, attempts: int | None):
    X, y = data
    settings = None if attempts is None else Settings(
        _env_file=None, adaptation_agent_max_attempts=attempts)
    return adapt_via_llm(
        client, LogisticRegression().fit(X, y), framework="sklearn",
        model_class="LogisticRegression", X=X, y=y, target_column="label",
        sandbox_timeout_s=30, sandbox_memory_mb=512, workdir=str(tmp_path / "llm"),
        sandbox_backend="subprocess", sandbox_docker_image="", settings=settings,
    )


def test_unsafe_code_goes_back_to_the_llm_and_the_fix_is_used(data, tmp_path, sandbox) -> None:
    llm = ScriptedLlm(UNSAFE, GOOD)
    with calls.recording() as recorded:
        candidate = _adapt(llm, data, tmp_path, attempts=3)

    assert candidate.engine == EngineKind.LLM_GENERATED
    assert [a["outcome"] for a in candidate.generation_attempts] == ["failed", "ok"]
    assert candidate.generation_attempts[0]["stage"] == "security_scan"
    # The second prompt shows the violation and the rejected code.
    assert "ATTEMPT 1 FAILED at the security_scan" in llm.prompts[1]
    assert "os" in llm.prompts[1] and "os.system('id')" in llm.prompts[1]
    assert "Attempts left including this one: 2" in llm.prompts[1]
    assert [c.extra["attempt"] for c in recorded] == [1, 2]
    # The rejected code never ran; only the fix did.
    assert len(sandbox) == 1
    assert len(joblib.load(candidate.artifact_path).predict(data[0])) == len(data[0])


def test_a_sandbox_crash_is_shown_with_its_traceback(data, tmp_path, sandbox) -> None:
    llm = ScriptedLlm(CRASHES, GOOD)
    candidate = _adapt(llm, data, tmp_path, attempts=2)

    assert candidate.generation_attempts[0]["stage"] == "sandbox"
    assert "inconsistent numbers of samples" in llm.prompts[1]
    # Each attempt runs in its own sandbox directory.
    assert sandbox[0] != sandbox[1] and sandbox[1].endswith("attempt-2")


def test_syntax_errors_are_fed_back(data, tmp_path, sandbox) -> None:
    llm = ScriptedLlm(NOT_PYTHON, GOOD)
    _adapt(llm, data, tmp_path, attempts=2)
    assert "not valid Python" in llm.prompts[1]


def test_when_every_attempt_fails_the_last_error_is_raised_with_all_attempts(
        data, tmp_path, sandbox) -> None:
    llm = ScriptedLlm(UNSAFE, CRASHES, NOT_PYTHON)
    with pytest.raises(UnsafeCodeError) as raised:
        _adapt(llm, data, tmp_path, attempts=3)

    attempts = raised.value.context["attempts"]
    assert [a["stage"] for a in attempts] == ["security_scan", "sandbox", "security_scan"]
    # The third prompt carries both earlier failures.
    assert "ATTEMPT 1 FAILED" in llm.prompts[2] and "ATTEMPT 2 FAILED" in llm.prompts[2]


def test_one_attempt_is_the_default_and_behaves_as_before(data, tmp_path, sandbox) -> None:
    for settings_attempts in (None, 1):
        llm = ScriptedLlm(UNSAFE)
        with pytest.raises(UnsafeCodeError) as raised:
            _adapt(llm, data, tmp_path, attempts=settings_attempts)
        assert len(llm.prompts) == 1 and "attempts" not in raised.value.context
    assert Settings(_env_file=None).adaptation_agent_max_attempts == 1
    assert not sandbox


def test_an_unreachable_llm_is_not_retried_by_the_loop(data, tmp_path, sandbox) -> None:
    llm = ScriptedLlm(CRASHES, LlmCircuitOpenError("open"))
    with pytest.raises(LlmCircuitOpenError):
        _adapt(llm, data, tmp_path, attempts=5)
    assert len(llm.prompts) == 2


def test_the_fed_back_detail_is_bounded(data, tmp_path, monkeypatch) -> None:
    def run(code, **_):
        raise SandboxExecutionError("sandbox script exited with a non-zero status",
                                    stderr="x" * 50_000)

    monkeypatch.setattr(llm_adapter, "run_sandboxed", run)
    llm = ScriptedLlm(GOOD, GOOD)
    with pytest.raises(SandboxExecutionError):
        _adapt(llm, data, tmp_path, attempts=2)
    assert len(llm.prompts[1]) < 10_000
