"""Phase I (Rules 9 and 19): the LLM-code sandbox, checked category by category - the static scan
refuses each escape route, the subprocess backend hands the child no secrets, and the Docker
backend and image carry the isolation flags and the non-root user."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from oran_adapt.core.errors import SandboxExecutionError, UnsafeCodeError
from oran_adapt.sandbox import runner
from oran_adapt.sandbox.security import check_code_safety

ROOT = Path(__file__).resolve().parents[2]

# One probe per Rule 9 category.
RULE9_PROBES = {
    "os.system": "import os\nos.system('id')",
    "subprocess": "import subprocess\nsubprocess.run(['id'])",
    "eval": "eval('1 + 1')",
    "exec": "exec('x = 1')",
    "dynamic import": "__import__('os')",
    "importlib": "import importlib\nimportlib.import_module('os')",
    "filesystem traversal": "open('../../etc/passwd')",
    "pandas file read": "import pandas as pd\npd.read_csv('/etc/passwd')",
    "network socket": "import socket",
    "network http": "import urllib.request",
    "network dataset download": "from sklearn.datasets import fetch_openml\nfetch_openml('x')",
    "env secrets": "import os\nos.environ['API_KEYS']",
    "process creation": "import multiprocessing",
    "threads": "import threading",
    "unpickling load": "import torch\ntorch.load('model.pt')",
    "dunder escape": "X.__class__.__subclasses__()",
    "frame escape": "g = (i for i in [])\ng.gi_frame.f_globals",
    "format-string escape": "'{0.__class__.__init__.__globals__}'.format(X)",
    "format_map escape": "'{x.__class__}'.format_map({'x': X})",
    "native code": "import ctypes",
}


@pytest.mark.parametrize("code", RULE9_PROBES.values(), ids=list(RULE9_PROBES))
def test_every_rule9_category_is_refused_before_execution(code: str) -> None:
    with pytest.raises(UnsafeCodeError):
        check_code_safety(code)


def test_ordinary_adaptation_code_still_passes_the_scan() -> None:
    check_code_safety(
        "from sklearn.linear_model import SGDRegressor\n"
        "import numpy as np\n"
        "model = SGDRegressor(random_state=0)\n"
        "model.fit(X, y)\n"
        "label = f'{len(X)} rows'\n"
        "result = model\n"
    )


def test_an_infinite_loop_passes_the_scan_and_is_stopped_by_the_timeout(tmp_path) -> None:
    # Loops cannot be ruled out statically; the runtime timeout is the control for them.
    code = "while True:\n    pass\nresult = current_model\n"
    check_code_safety(code)
    with pytest.raises(SandboxExecutionError):
        runner.run_in_sandbox(
            code,
            current_model=None,
            X=pd.DataFrame({"a": [1.0]}),
            y=pd.Series([1.0]),
            timeout_s=2,
            memory_mb=512,
            workdir=str(tmp_path),
        )


def test_the_subprocess_sandbox_inherits_no_secrets(monkeypatch) -> None:
    monkeypatch.setenv("API_KEYS", "secret-digest")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    env = runner._sandbox_env()

    assert set(env) <= set(runner._INHERITED_ENV_VARS)
    assert "secret" not in " ".join(env.values())


def test_the_docker_backend_runs_isolated(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "stopped by test")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(SandboxExecutionError):
        runner.run_in_docker(
            "result = current_model",
            current_model=None,
            X=pd.DataFrame({"a": [1.0]}),
            y=pd.Series([1.0]),
            timeout_s=5,
            memory_mb=256,
            workdir=str(tmp_path),
            image="oran-adapt-sandbox:latest",
        )

    cmd = calls[0]
    joined = " ".join(cmd)
    for flag in (
        "--network none",
        "--memory 256m",
        "--memory-swap 256m",
        "--pids-limit 128",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    ):
        assert flag in joined
    assert "-e" not in cmd and "--env" not in cmd and "--privileged" not in cmd
    assert f"{os.path.abspath(tmp_path)}:/sandbox" in cmd


def test_the_sandbox_image_runs_as_non_root_and_holds_no_project_code() -> None:
    dockerfile = (ROOT / "docker" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    instructions = [
        line.split()[0] for line in dockerfile.splitlines() if line and line[0].isupper()
    ]

    assert "USER 10001" in dockerfile
    assert instructions.index("USER") > instructions.index("RUN")
    assert "COPY" not in instructions and "ADD" not in instructions
