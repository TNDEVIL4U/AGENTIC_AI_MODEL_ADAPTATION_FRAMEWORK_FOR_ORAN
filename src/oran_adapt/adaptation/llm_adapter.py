"""Member 3 - LLM adapter: the fallback path when the engine registry has no built-in engine for
a (framework, strategy) combination (select_engine/run_engine raised UnsupportedAdaptationError).
Instead of giving up, ask the LLM to write the adaptation code itself - but that code is never
trusted and never runs in this process. It is first statically checked by sandbox.security, then
executed by sandbox.runner in an isolated subprocess; only the fitted model object it returns
ever crosses back.

With ADAPTATION_AGENT_MAX_ATTEMPTS above 1 this is a write -> check -> run -> fix loop: code the
security scan rejects, or that fails in the sandbox, goes back to the LLM with the error (the
scan's violations, the syntax error, the sandbox's stderr tail, the limit it hit) and the LLM
writes a corrected version, until an attempt succeeds or the attempts run out. Every attempt is
checked and sandboxed exactly like the first; the loop only decides what the LLM is shown next.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import joblib
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core import metrics
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import SandboxExecutionError, UnsafeCodeError
from oran_adapt.llm import calls
from oran_adapt.llm.client import LlmClient
from oran_adapt.llm.prompts import ADAPTATION_CODE, get_prompt
from oran_adapt.sandbox.runner import SandboxLimits, run_sandboxed
from oran_adapt.sandbox.security import check_code_safety

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger(__name__)


def _extract_code(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _build_user_prompt(
    framework: str, model_class: str, feature_names: list[str], target_column: str
) -> str:
    return (
        f"framework: {framework}\n"
        f"model_class: {model_class}\n"
        f"feature_names: {feature_names}\n"
        f"target_column: {target_column}\n"
        f"n_features: {len(feature_names)}\n"
    )


# How much of a failed attempt goes back to the LLM: its code, and the error's detail.
_FEEDBACK_CODE_CHARS = 6000
_FEEDBACK_DETAIL_CHARS = 2000
# The error context keys worth showing the LLM (stderr is the sandbox traceback).
_FEEDBACK_KEYS = ("violations", "cause", "stderr", "returncode", "timeout_s", "memory_mb",
                  "rss_mb", "format", "types", "cls")


def _failure(exc: UnsafeCodeError | SandboxExecutionError) -> dict[str, Any]:
    stage = "security_scan" if isinstance(exc, UnsafeCodeError) else "sandbox"
    detail = {k: exc.context[k] for k in _FEEDBACK_KEYS if k in exc.context}
    text = str(detail)
    if len(text) > _FEEDBACK_DETAIL_CHARS:
        text = "..." + text[-_FEEDBACK_DETAIL_CHARS:]
    return {"stage": stage, "error": exc.message, "detail": text}


def _fix_prompt(base: str, attempts: list[tuple[str, dict[str, Any]]], left: int) -> str:
    parts = [base]
    for n, (code, failure) in enumerate(attempts, start=1):
        parts.append(
            f"\nATTEMPT {n} FAILED at the {failure['stage']}: {failure['error']}\n"
            f"detail: {failure['detail']}\n"
            f"code of attempt {n}:\n{code[:_FEEDBACK_CODE_CHARS]}\n"
        )
    parts.append(
        f"\nFix the cause of the failure above and write the complete corrected code. "
        f"Attempts left including this one: {left}."
    )
    return "".join(parts)


def adapt_via_llm(
    client: LlmClient,
    current_model: object,
    *,
    framework: str,
    model_class: str,
    X: pd.DataFrame,
    y: pd.Series,
    target_column: str,
    sandbox_timeout_s: int,
    sandbox_memory_mb: int,
    workdir: str,
    sandbox_backend: str,
    sandbox_docker_image: str | None,
    skops_trusted_types: tuple[str, ...] | list[str] | None = None,
    sandbox_limits: SandboxLimits | None = None,
    settings: Settings | None = None,
) -> CandidateModel:
    """Raises LlmUnavailableError if the LLM can't be reached, UnsafeCodeError if its code fails
    the security scan, or SandboxExecutionError if the (safe) code fails to run - after the last
    of ``settings.adaptation_agent_max_attempts`` attempts, each failure before it going back to
    the LLM to be fixed; with several attempts the raised error's context carries all of them.
    The caller (Phase 9 orchestrator) decides what "no adaptation possible" means for the job.
    The system prompt is the versioned ``adaptation-code`` prompt."""
    feature_names = list(X.columns)
    prompt = get_prompt(ADAPTATION_CODE, settings)
    base = _build_user_prompt(framework, model_class, feature_names, target_column)
    max_attempts = settings.adaptation_agent_max_attempts if settings is not None else 1
    failed: list[tuple[str, dict[str, Any]]] = []
    trace: list[dict[str, Any]] = []

    for attempt in range(1, max_attempts + 1):
        user = base if not failed else _fix_prompt(base, failed, max_attempts - attempt + 1)
        # LlmUnavailableError is not retried here: the guard already retried the call.
        raw = calls.ask(client, prompt, user, purpose="adaptation", attempt=attempt)
        code = _extract_code(raw)
        # Each retry gets a fresh sandbox directory, so no output of a failed attempt is read.
        attempt_dir = workdir if attempt == 1 else os.path.join(workdir, f"attempt-{attempt}")
        try:
            try:
                check_code_safety(code)
            except UnsafeCodeError:
                metrics.SANDBOX_FAILURES.labels("unsafe_code").inc()
                raise
            try:
                model = run_sandboxed(
                    code,
                    current_model=current_model,
                    X=X,
                    y=y,
                    timeout_s=sandbox_timeout_s,
                    memory_mb=sandbox_memory_mb,
                    workdir=attempt_dir,
                    backend=sandbox_backend,
                    docker_image=sandbox_docker_image,
                    skops_trusted_types=skops_trusted_types,
                    limits=sandbox_limits,
                )
            except SandboxExecutionError:
                metrics.SANDBOX_FAILURES.labels("execution").inc()
                raise
        except (UnsafeCodeError, SandboxExecutionError) as exc:
            failure = _failure(exc)
            trace.append({"attempt": attempt, "outcome": "failed", **failure})
            if attempt == max_attempts:
                if max_attempts > 1:
                    exc.context["attempts"] = trace
                raise
            logger.warning(
                "LLM adaptation attempt failed, asking the LLM to fix it",
                extra={"attempt": attempt, "stage": failure["stage"], "error": failure["error"]},
            )
            failed.append((code, failure))
            continue
        trace.append({"attempt": attempt, "outcome": "ok"})
        break

    os.makedirs(workdir, exist_ok=True)
    artifact_path = os.path.join(workdir, "model.joblib")
    joblib.dump(model, artifact_path)

    return CandidateModel(
        engine=EngineKind.LLM_GENERATED,
        framework=framework,
        model_class=type(model).__name__,
        artifact_path=artifact_path,
        metrics={},
        n_train_rows=len(X),
        feature_names=feature_names,
        target_column=target_column,
        generation_attempts=trace,
    )
