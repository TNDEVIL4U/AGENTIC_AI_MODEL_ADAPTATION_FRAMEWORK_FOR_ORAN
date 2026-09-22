"""Phase 0: the integrated package skeleton exists and is importable."""

import importlib
from pathlib import Path

import pytest

import oran_adapt

SUBPACKAGES = [
    "core", "db", "registry", "analysis", "decision", "adaptation",
    "validation", "orchestrator", "llm", "sandbox", "api",
]
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name: str) -> None:
    assert importlib.import_module(f"oran_adapt.{name}")


def test_single_application_not_three_projects() -> None:
    assert not any((ROOT / d).exists() for d in ("member1", "member2", "member3"))
    assert oran_adapt.__version__


def test_env_example_has_no_secrets() -> None:
    text = (ROOT / ".env.example").read_text()
    for line in text.splitlines():
        if line.startswith(("ANTHROPIC_API_KEY", "GEMINI_API_KEY")):
            assert line.split("=", 1)[1].split("#")[0].strip() == ""
