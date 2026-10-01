"""Hardening Phase 2 acceptance: the model registry abstraction, checked end to end.

Run by scripts/verify.sh 2 after lint, the import-boundary test and the scoped tests.

1. The registry conformance suite passes for the filesystem adapter (filesystem and fsspec
   artifact stores) and the MLflow reference adapter against the local stack (sqlite MLflow).
2. The SageMaker and Vertex adapters pass it against the API emulators in
   tests/unit/registry_emulators.py - emulators, so unverified against AWS/GCP.
3. No mlflow import outside adapters/registry/mlflow/ (static scan), and a filesystem-registry,
   native-format deployment publishes, resolves a model URI, downloads and loads a model in a
   child process where importing mlflow is impossible.
4. Model loading is out of the registry: neither the port nor any registry adapter loads models.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # registry_emulators, test_import_boundary


def _settings(tmp: Path, **overrides: object):
    from oran_adapt.core.config import Settings

    values: dict[str, object] = {
        "database_url": f"sqlite:///{(tmp / 'app.db').as_posix()}",
        "mlflow_tracking_uri": f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        "artifact_workdir": str(tmp / "work"),
        "registry_fs_root": str(tmp / "registry"),
        "artifact_store_root": str(tmp / "store"),
        "log_json": False,
        "auth_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _conformance(registry: object, tmp: Path) -> str:
    from oran_adapt.conformance.registry import Context, run

    return f"{len(run(registry, Context(tmp / 'conformance')))} checks"  # type: ignore[arg-type]


def local_conformance(tmp: Path) -> str:
    from oran_adapt.bootstrap import build_registry

    results = []
    for label, overrides in (
        ("filesystem+filesystem-store", {"registry_backend": "filesystem"}),
        (
            "filesystem+fsspec(memory)",
            {
                "registry_backend": "filesystem",
                "artifact_store_backend": "fsspec",
                "artifact_store_url": "memory://oran-phase2-acceptance",
            },
        ),
        ("mlflow(sqlite)", {"registry_backend": "mlflow"}),
    ):
        root = tmp / label.split("(")[0].replace("+", "-")
        root.mkdir()
        registry = build_registry(_settings(root, **overrides))
        results.append(f"{label}: {_conformance(registry, root)}")
    return "; ".join(results)


def emulated_cloud_conformance(tmp: Path) -> str:
    import test_phase2_registry as t

    results = []
    for label, make in (
        ("sagemaker", t.sagemaker_registry),
        ("vertex", t.vertex_registry),
    ):
        root = tmp / label
        root.mkdir()
        results.append(f"{label}: {_conformance(make(_settings(root)), root)}")
    return "; ".join(results) + " (emulator; unverified against AWS/GCP)"


CHILD = r"""
import json, sys
sys.modules["mlflow"] = None  # any import of mlflow now raises
from pathlib import Path
import numpy as np
from sklearn.linear_model import LinearRegression
from oran_adapt.bootstrap import build_model_handler, build_registry
from oran_adapt.core.config import Settings
from oran_adapt.core.model_uri import ModelUri
from oran_adapt.registry.publishing import publish_model

tmp = Path(sys.argv[1])
settings = Settings(
    _env_file=None, registry_backend="filesystem", model_format="native",
    registry_fs_root=str(tmp / "registry"), artifact_store_root=str(tmp / "store"),
    artifact_workdir=str(tmp / "work"), log_json=False,
)
registry, handler = build_registry(settings), build_model_handler(settings)
x = np.arange(20, dtype=float).reshape(10, 2)
model = LinearRegression().fit(x, x.sum(axis=1))
version = publish_model(registry, handler, "kpi", model, framework="sklearn", workdir=str(tmp))
registry.set_alias("kpi", "live", version)
resolved = ModelUri.parse("model://kpi@live").resolve(registry)
local = registry.download_artifacts("kpi", resolved.version, str(tmp / "dl"))
loaded = handler.load(local, "sklearn")
same = bool(np.allclose(loaded.predict(x), model.predict(x)))
print(json.dumps({"version": resolved.version, "same_predictions": same}))
"""


def runs_without_mlflow(tmp: Path) -> str:
    from test_import_boundary import violations

    found = [v for v in violations() if v.endswith("imports mlflow")]
    assert not found, f"mlflow imported outside adapters/registry/mlflow/: {found}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REGISTRY_", "MODEL_"))}
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp)],
        cwd=tmp, env=env, check=False, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, f"child failed: {proc.stderr[-1500:]}"
    body = json.loads(proc.stdout.strip().splitlines()[-1])
    assert body == {"version": "1", "same_predictions": True}, body
    return "static scan clean; publish -> model://kpi@live -> download -> load with mlflow blocked"


def loading_is_out_of_the_registry(tmp: Path) -> str:
    from oran_adapt.ports import ModelRegistryPort

    assert not hasattr(ModelRegistryPort, "load_model"), "the registry port still loads models"
    offenders = []
    for path in sorted((ROOT / "src" / "oran_adapt" / "adapters" / "registry").rglob("*.py")):
        if path.name == "flavors.py":  # the mlflow-flavors model handler, not a registry
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("load_model", "log_model"):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not offenders, f"registry adapters that still load/log models: {offenders}"
    return "ModelRegistryPort and every registry adapter are free of load_model/log_model"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("conformance suite green: filesystem and MLflow, local stack", local_conformance),
    ("conformance suite green: SageMaker and Vertex, emulated", emulated_cloud_conformance),
    ("no mlflow outside its adapter; a non-MLflow deployment runs", runs_without_mlflow),
    ("model loading belongs to the handler port", loading_is_out_of_the_registry),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase2-", ignore_cleanup_errors=True) as tmp:
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}")
            else:
                print(f"PASS  {name}\n      {detail}")
    print(f"\nphase 2 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
