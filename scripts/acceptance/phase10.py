"""Hardening Phase 10 acceptance: the LLM is optional and fenced.

Run by scripts/verify.sh 10 after lint, the import-boundary test and the scoped tests.

1. Offline: LLM_ENABLED is off by default (settings, .env.example, build_llm), and the whole
   pipeline runs to REGISTERED while every outbound connection and name lookup fails the test;
   MLflow's telemetry is off.
2. Degradation: an unreachable provider, an open circuit, a refused cap, invalid output and
   unsafe generated code each fall back to the rules; the reason is on the decision, the job's
   llm_calls, the audit trail and the fallback counter, and no job fails for it.
3. Caps: the input cap, the token budget and the cost budget (shared across processes through
   the llm_usage table) are enforced before anything is sent; an unreadable ledger refuses.
4. Adapters: anthropic, gemini, openai-compatible and the template pass the LLM conformance
   suite against local wire-format doubles; retries back off, the breaker opens and half-opens,
   prompts are versioned and stamped, and HTTP clients come only from the outbound policy.
5. The live provider smoke exists, is heavy, and is skipped unless ORAN_LLM_LIVE=1.

The live services (Anthropic, Gemini, any OpenAI-compatible server) are unverified locally.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 10 test doubles
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gate import run_selection

PHASE10 = TESTS / "test_phase10_llm.py"


def _run(selection: str, *extra: str, target: Path = PHASE10) -> None:
    run_selection(target, selection, *extra)


def offline_by_default(tmp: Path) -> str:
    from oran_adapt.bootstrap import build_llm
    from oran_adapt.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.llm_enabled is False and build_llm(settings) is None
    assert settings.mlflow_telemetry is False
    example = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active = {line.split("=", 1)[0]: line.split("=", 1)[1].split("#")[0].strip()
              for line in example if "=" in line and not line.startswith("#")}
    assert active.get("LLM_ENABLED") == "false", ".env.example must keep LLM_ENABLED=false"
    assert active.get("LLM_PROVIDER") == "none", ".env.example must keep LLM_PROVIDER=none"
    _run("off_by_default or without_a_provider or egress or telemetry")
    return "LLM off by default; pipeline REGISTERED with zero outbound attempts"


def degradation(tmp: Path) -> str:
    _run("no_llm_decides or unreachable_provider or recorded_fallback or open_circuit "
         "or retrains_instead or valid_llm_choice")
    return "unavailable, circuit_open, budget_exceeded, invalid_output, unsafe_code -> rules"


def caps(tmp: Path) -> str:
    _run("input_cap or token_budget or cost_budget or unreadable_ledger")
    return "input cap, token budget, cost budget (memory and SQL ledgers) enforced"


def adapters_guard_prompts(tmp: Path) -> str:
    _run("conformance or retries or last_failure or unexpected or breaker or circuit_opens "
         "or prompts or outbound or wrapped_in_the_guard")
    return "3 providers + template conformant; retries, breaker, prompts, outbound policy"


def live_smoke_is_heavy_and_off(tmp: Path) -> str:
    tree = ast.parse(PHASE10.read_text(encoding="utf-8"))
    found = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "test_live_provider_smoke"]
    assert found, "test_live_provider_smoke is missing"
    marks = [ast.unparse(d) for d in found[0].decorator_list]
    assert "pytest.mark.heavy" in marks, marks
    assert any("skipif" in m and "ORAN_LLM_LIVE" in m for m in marks), marks
    return "heavy, skipped unless ORAN_LLM_LIVE=1"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("offline by default: egress-blocked end to end", offline_by_default),
    ("every LLM failure degrades to the rules, reason recorded", degradation),
    ("input, token and cost caps", caps),
    ("provider conformance, guard and prompts", adapters_guard_prompts),
    ("live provider smoke is heavy and off by default", live_smoke_is_heavy_and_off),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase10-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 10 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
