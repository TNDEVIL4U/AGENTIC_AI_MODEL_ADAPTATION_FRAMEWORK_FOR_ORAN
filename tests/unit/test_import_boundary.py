"""Zero vendor branching: only oran_adapt.adapters may import a vendor SDK.

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
        "google.genai",
        "hvac",
        "kserve",
        "kubernetes",
        "mlflow",
        "openai",
    }
)


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


def violations(root: Path = SRC) -> list[str]:
    adapters = root / "adapters"
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if adapters in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in _imports(tree):
            sdk = _vendor(module)
            if sdk:
                out.append(f"{path.relative_to(root.parent)}:{lineno}: imports {sdk}")
    return sorted(set(out))


def test_no_vendor_sdk_outside_adapters() -> None:
    found = violations()
    assert not found, "vendor SDK imported outside oran_adapt.adapters:\n" + "\n".join(found)


def test_the_check_catches_a_violation(tmp_path: Path) -> None:
    pkg = tmp_path / "oran_adapt"
    (pkg / "adapters").mkdir(parents=True)
    (pkg / "domain.py").write_text(
        "def f():\n    from google import genai\n    import mlflow.sklearn\n", encoding="utf-8"
    )
    (pkg / "adapters" / "ok.py").write_text("import mlflow\n", encoding="utf-8")
    found = violations(pkg)
    assert len(found) == 2
    assert all("domain.py" in line for line in found)
