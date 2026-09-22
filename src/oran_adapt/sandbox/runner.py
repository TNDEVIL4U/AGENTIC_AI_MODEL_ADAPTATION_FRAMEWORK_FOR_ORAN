"""Member 3 - sandbox execution: runs security-checked adaptation code in an isolated
subprocess with a wall-clock timeout and (on POSIX) a hard address-space ceiling, and hands
inputs/outputs across the process boundary via pickle files rather than a shared object graph -
the point of a sandbox is that a compromised or merely buggy script's blast radius stops at the
subprocess, never touching this process's memory, environment or open handles.

This is the "restricted subprocess" backend the Phase 0 audit documents as weaker than Docker
isolation (no filesystem/network namespace, no cgroup, and no memory ceiling at all on Windows,
where `resource.setrlimit` does not exist) but real and exercised in tests, unlike the Docker
backend which cannot run on this machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

import joblib
import pandas as pd

from oran_adapt.core.errors import SandboxExecutionError

_SCRIPT_TEMPLATE = """\
import joblib

inputs = joblib.load({inputs_path!r})
current_model = inputs["current_model"]
X = inputs["X"]
y = inputs["y"]

{code}

result = adapt(current_model, X, y)
joblib.dump(result, {output_path!r})
"""

# Only what the interpreter and its installed packages actually need to start correctly - never
# the parent's full environment, which may hold API keys, DB credentials, etc. VIRTUAL_ENV and
# PYTHONPATH cover venv package resolution; APPDATA/USERPROFILE/LOCALAPPDATA cover Python's user
# site-packages directory (site.getusersitepackages()), which some dependencies here are
# installed into rather than the venv.
_INHERITED_ENV_VARS = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "APPDATA",
    "USERPROFILE",
    "LOCALAPPDATA",
)


def _sandbox_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _INHERITED_ENV_VARS}


def _memory_limit_preexec(memory_mb: int) -> Callable[[], None] | None:
    if os.name != "posix":
        return None

    def _apply() -> None:
        import resource

        limit_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    return _apply


def run_in_sandbox(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
) -> object:
    """Runs ``code`` (which must define ``adapt(current_model, X, y) -> model``) in a fresh
    subprocess and returns the model it produced. Raises SandboxExecutionError on timeout,
    resource-limit violation, non-zero exit, or a missing/unreadable result - never lets a
    subprocess failure surface as a raw OSError/TimeoutExpired to the caller."""
    os.makedirs(workdir, exist_ok=True)
    inputs_path = os.path.join(workdir, "inputs.joblib")
    output_path = os.path.join(workdir, "output.joblib")
    script_path = os.path.join(workdir, "script.py")

    joblib.dump({"current_model": current_model, "X": X, "y": y}, inputs_path)
    script = _SCRIPT_TEMPLATE.format(code=code, inputs_path=inputs_path, output_path=output_path)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_sandbox_env(),
            preexec_fn=_memory_limit_preexec(memory_mb),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxExecutionError(
            "sandbox execution exceeded the timeout", timeout_s=timeout_s
        ) from exc

    if proc.returncode != 0:
        raise SandboxExecutionError(
            "sandbox script exited with a non-zero status",
            returncode=proc.returncode,
            stderr=proc.stderr[-4000:],
        )

    if not os.path.exists(output_path):
        raise SandboxExecutionError("sandbox script produced no output artifact")

    try:
        return joblib.load(output_path)
    except Exception as exc:
        raise SandboxExecutionError(
            "sandbox output artifact could not be loaded", cause=str(exc)
        ) from exc


def run_in_docker(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
    image: str,
) -> object:
    """The Phase 0 audit's "stronger isolation" backend: a real container boundary (its own
    filesystem and network namespace, an enforced memory cgroup on every OS - not just POSIX -
    and no shared PID namespace with this process) instead of the subprocess backend's bare
    resource limits. Same contract as `run_in_sandbox` - same script template, same input/output
    hand-off via files in `workdir` - just executed by `docker run` against `image` instead of a
    local Python subprocess.

    Requires the `docker` CLI and a running daemon on the host. Neither is available on the
    machine this was written on (confirmed via `docker --version` returning "command not
    found"), so this function is written in full per the Phase 0 audit's documented plan but has
    never actually been executed - only `tests/integration/test_docker_sandbox.py`, which skips
    itself when `docker` is absent, exercises it, and only when Docker is installed."""
    os.makedirs(workdir, exist_ok=True)
    inputs_path = os.path.join(workdir, "inputs.joblib")
    output_path = os.path.join(workdir, "output.joblib")
    script_path = os.path.join(workdir, "script.py")

    joblib.dump({"current_model": current_model, "X": X, "y": y}, inputs_path)
    script = _SCRIPT_TEMPLATE.format(
        code=code, inputs_path="/sandbox/inputs.joblib", output_path="/sandbox/output.joblib"
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        f"{memory_mb}m",
        "--memory-swap",
        f"{memory_mb}m",
        "--pids-limit",
        "128",
        "-v",
        f"{os.path.abspath(workdir)}:/sandbox",
        "-w",
        "/sandbox",
        image,
        "python",
        "script.py",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SandboxExecutionError(
            "docker sandbox execution exceeded the timeout", timeout_s=timeout_s
        ) from exc
    except OSError as exc:
        raise SandboxExecutionError(
            "docker is not available on this host", cause=str(exc)
        ) from exc

    if proc.returncode != 0:
        raise SandboxExecutionError(
            "docker sandbox script exited with a non-zero status",
            returncode=proc.returncode,
            stderr=proc.stderr[-4000:],
        )

    if not os.path.exists(output_path):
        raise SandboxExecutionError("docker sandbox script produced no output artifact")

    try:
        return joblib.load(output_path)
    except Exception as exc:
        raise SandboxExecutionError(
            "docker sandbox output artifact could not be loaded", cause=str(exc)
        ) from exc


def run_sandboxed(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
    backend: str,
    docker_image: str = "",
) -> object:
    """Dispatches to `run_in_docker` or `run_in_sandbox` by `backend` ("docker" or
    "subprocess") - the one entry point `adaptation.llm_adapter` calls, so it never chooses
    between the two backends itself."""
    if backend == "docker":
        return run_in_docker(
            code,
            current_model=current_model,
            X=X,
            y=y,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            workdir=workdir,
            image=docker_image,
        )
    return run_in_sandbox(
        code,
        current_model=current_model,
        X=X,
        y=y,
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        workdir=workdir,
    )
