"""Member 3 - LLM adapter: the fallback path when the engine registry has no built-in engine for
a (framework, strategy) combination (select_engine/run_engine raised UnsupportedAdaptationError).
Instead of giving up, ask the LLM to write the adaptation code itself - but that code is never
trusted and never runs in this process. It is first statically checked by sandbox.security, then
executed by sandbox.runner in an isolated subprocess; only the fitted model object it returns
ever crosses back.
"""

from __future__ import annotations

import os

import joblib
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core import metrics
from oran_adapt.core.enums import EngineKind
from oran_adapt.core.errors import SandboxExecutionError, UnsafeCodeError
from oran_adapt.llm.client import LlmClient
from oran_adapt.sandbox.runner import run_sandboxed
from oran_adapt.sandbox.security import check_code_safety

_SYSTEM_PROMPT = (
    "You write Python adaptation code for an O-RAN model-adaptation pipeline. "
    "Define exactly one top-level function `adapt(current_model, X, y)` that returns a trained "
    "model object usable the same way as current_model, trained on X (a pandas DataFrame of "
    "features) and y (a pandas Series of the target). Continue training current_model in place "
    "when that is reasonable for its framework; otherwise fit a fresh model of the same kind. "
    "You may only import from: numpy, pandas, sklearn, xgboost, torch, math, json. "
    "Never use eval, exec, compile, __import__, open, input, os, sys, subprocess, socket, or any "
    "dunder attribute such as __globals__ or __subclasses__ - the code runs in a restricted "
    "sandbox that rejects all of these before execution. "
    "Respond with ONLY the Python code defining `adapt`, no markdown fences, no prose."
)


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
    sandbox_backend: str = "subprocess",
    sandbox_docker_image: str = "",
) -> CandidateModel:
    """Raises LlmUnavailableError if the LLM can't be reached, UnsafeCodeError if its code fails
    the security scan, or SandboxExecutionError if the (safe) code fails to run. Never catches
    any of these - the caller (Phase 9 orchestrator) decides what "no adaptation possible" means
    for the job."""
    feature_names = list(X.columns)
    raw = client.complete(
        system=_SYSTEM_PROMPT,
        prompt=_build_user_prompt(framework, model_class, feature_names, target_column),
    )
    code = _extract_code(raw)
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
            workdir=workdir,
            backend=sandbox_backend,
            docker_image=sandbox_docker_image,
        )
    except SandboxExecutionError:
        metrics.SANDBOX_FAILURES.labels("execution").inc()
        raise

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
    )
