"""Zero vendor branching: only oran_adapt.adapters may import a vendor SDK, and an SDK with a
home in SDK_HOMES only from there (mlflow only from adapters/registry/mlflow/).

Domain code talks to ports (oran_adapt.ports); adapters are resolved once at the composition
root. This test fails the build as soon as a vendor SDK import appears anywhere else, including
imports nested inside functions and ``TYPE_CHECKING`` blocks."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

SRC = Path(__file__).resolve().parents[2] / "src" / "oran_adapt"

# Top-level distributions that talk to one vendor's service or product.
VENDOR_SDKS = frozenset(
    {
        "anthropic",
        "boto3",
        "botocore",
        "confluent_kafka",
        "google.auth",
        "google.cloud",
        "google.genai",
        "hvac",
        "kserve",
        "kubernetes",
        "mlflow",
        "openai",
        "sagemaker",
    }
)

# SDK -> the adapter packages / modules (relative to oran_adapt) that alone may import it.
SDK_HOMES: dict[str, tuple[str, ...]] = {
    "mlflow": ("adapters/registry/mlflow",),
    "boto3": (
        "adapters/registry/sagemaker.py",
        "adapters/deployment/sagemaker.py",
        "adapters/notify_brokers.py",
        "adapters/datasets_cloud.py",
    ),
    "botocore": (
        "adapters/registry/sagemaker.py",
        "adapters/notify_brokers.py",
        "adapters/datasets_cloud.py",
    ),
    "confluent_kafka": ("adapters/kafka_cdc.py", "adapters/notify_brokers.py"),
    "google.auth": ("adapters/registry/vertex.py",),
}


def _vendor(module: str) -> str | None:
    for sdk in VENDOR_SDKS:
        if module == sdk or module.startswith(sdk + "."):
            return sdk
    return None


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
            # ``from google import genai`` names the SDK only through the imported name.
            found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append((node.lineno, node.args[0].value))
    return found


def _allowed(root: Path, path: Path, sdk: str) -> bool:
    homes = SDK_HOMES.get(sdk)
    if homes is None:
        return (root / "adapters") in path.parents
    return any(path == root / home or (root / home) in path.parents for home in homes)


def violations(root: Path = SRC) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in _imports(tree):
            sdk = _vendor(module)
            if sdk and not _allowed(root, path, sdk):
                rel = path.relative_to(root.parent).as_posix()
                out.append(f"{rel}:{lineno}: imports {sdk}")
    return sorted(set(out))


def test_no_vendor_sdk_outside_its_adapter() -> None:
    found = violations()
    assert not found, "vendor SDK imported outside its adapter:\n" + "\n".join(found)


def test_the_check_catches_a_violation(tmp_path: Path) -> None:
    pkg = tmp_path / "oran_adapt"
    (pkg / "adapters" / "registry" / "mlflow").mkdir(parents=True)
    (pkg / "domain.py").write_text(
        "def f():\n    from google import genai\n    import mlflow.sklearn\n", encoding="utf-8"
    )
    (pkg / "adapters" / "ok.py").write_text("import anthropic\n", encoding="utf-8")
    (pkg / "adapters" / "registry" / "mlflow" / "ok.py").write_text(
        "import mlflow\n", encoding="utf-8"
    )
    (pkg / "adapters" / "other.py").write_text("from mlflow import MlflowClient\n", "utf-8")
    found = violations(pkg)
    assert len(found) == 3, found
    assert sum("domain.py" in line for line in found) == 2
    assert any("adapters/other.py" in line for line in found)
