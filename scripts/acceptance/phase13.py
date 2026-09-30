"""Hardening Phase 13 acceptance: tests and conformance as a gate.

Run by scripts/verify.sh 13 after lint, the import-boundary test and the scoped tests.

1. Every installed adapter passes its port's conformance suite: every test named in
   tests/unit/conformance_coverage.py runs, and each (port, adapter) has passing cases and no
   failing one; an adapter with no suite coverage fails the gate unless it has an unexpired,
   owned exemption.
2. Mutation testing: every mutant of the validation gate and the job state machine is killed
   or documented as equivalent (score 1.0), and the originals are restored.
3. The scenario matrix names every scenario, and all its non-heavy tests pass in under 5 min.
4. No skip without an owner and an expiry, checked in the source and when a skip happens.
5. testcontainers (real PostgreSQL and Kafka) runs in CI's ``containers`` job; the laptop has
   no Docker daemon, so it is unverified locally.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conformance_coverage as coverage
import scenario_matrix
from _gate import run_selection

import skip_policy

SCENARIO_BUDGET_S = 300.0
WORKERS = "2"


def _pytest(node_ids: list[str], junit: Path) -> dict[str, bool]:
    """Run ``node_ids`` (-n 2); (file stem::case name) -> passed, from the JUnit report."""
    import pytest

    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", "-n", WORKERS,
                        "--junitxml", str(junit), *node_ids])
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(getattr(handler, "stream", None), "closed", False):
            root.removeHandler(handler)
    outcomes: dict[str, bool] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        module = case.get("classname", "").rsplit(".", 1)[-1]
        failed = any(child.tag in {"failure", "error", "skipped"} for child in case)
        outcomes[f"{module}::{case.get('name', '')}"] = not failed
    assert code in (0, 1), f"pytest could not run the selection (exit {code})"
    return outcomes


def conformance(tmp: Path) -> str:
    from oran_adapt import plugins

    installed = [(port, name) for port in coverage.SUITES
                 for name in plugins.adapters(port)]
    today = datetime.now(UTC).date()
    node_ids = sorted({f"tests/unit/{path}::{func}"
                       for entries in coverage.COVERAGE.values() for path, func, _ in entries})
    outcomes = _pytest(node_ids, tmp / "conformance.xml")
    problems, cases = [], 0
    for port, adapter in installed:
        tests = coverage.covering(port, adapter)
        if not tests:
            exemption = coverage.EXEMPT.get((port, adapter))
            if exemption is None or exemption.expires < today:
                problems.append(f"{port}/{adapter}: no conformance test and no live exemption")
            continue
        matched = {key: ok for key, ok in outcomes.items()
                   for path, func, param in tests
                   if key.split("::")[0] == Path(path).stem
                   and coverage.case_matches(key.split("::")[1], func, param)}
        if not matched:
            problems.append(f"{port}/{adapter}: its conformance cases did not run")
        failed = sorted(k for k, ok in matched.items() if not ok)
        if failed:
            problems.append(f"{port}/{adapter}: {failed}")
        cases += len(matched)
    assert not problems, problems
    ports = len({port for port, _ in installed})
    return f"{len(installed)} adapters on {ports} ports, {cases} conformance cases passed"


def _mutation_module():
    spec = importlib.util.spec_from_file_location("oran_mutation", ROOT / "scripts" / "mutation.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mutation(tmp: Path) -> str:
    mutator = _mutation_module()
    kills = mutator.load_kills()
    parts = []
    for target in sorted(mutator.TARGETS):
        mutants = mutator.run(target, kills)
        survivors = [f"{m.function}:{m.line} {m.operator}" for m in mutants
                     if m.killed_by is None and not m.equivalent]
        assert not survivors, f"{target}: surviving mutants {survivors}"
        assert mutator.score(mutants) == 1.0
        equivalent = sum(1 for m in mutants if m.killed_by is None)
        parts.append(f"{target} {len(mutants)} mutants ({equivalent} equivalent)")
    run_selection(TESTS / "test_phase13_mutation.py", "mutant or kill or restored or operator "
                  "or unmutated")
    return "; ".join(parts) + "; score 1.0"


def scenarios(tmp: Path) -> str:
    fast = scenario_matrix.fast_node_ids(ROOT)
    started = time.monotonic()
    outcomes = _pytest([*fast, "tests/unit/test_phase13_scenarios.py"], tmp / "scenarios.xml")
    elapsed = time.monotonic() - started
    failed = sorted(k for k, ok in outcomes.items() if not ok)
    assert not failed, f"scenario tests did not pass: {failed}"
    for node_id in fast:
        rel, func = node_id.split("::")
        key = f"{Path(rel).stem}::{func}"
        assert any(k == key or k.startswith(key.split("[")[0] + "[") for k in outcomes), \
            f"{node_id} did not run"
    assert elapsed < SCENARIO_BUDGET_S, f"non-heavy scenarios took {elapsed:.0f}s"
    return (f"{len(scenario_matrix.SCENARIOS)} scenarios, {len(fast)} non-heavy tests "
            f"({len(outcomes)} cases) in {elapsed:.0f}s < {SCENARIO_BUDGET_S:.0f}s")


def skips(tmp: Path) -> str:
    today = datetime.now(UTC).date()
    sites = [(path, line, reason) for path in sorted((ROOT / "tests").rglob("*.py"))
             for line, _, reason in skip_policy.skip_sites(path)]
    broken = [f"{p.name}:{line}" for p, line, reason in sites
              if skip_policy.problem(reason, today) is not None]
    assert not broken, f"skips without a live owner and expiry: {broken}"
    run_selection(TESTS / "test_phase13_skip_policy.py", "skip or tag or scan")
    return f"{len(sites)} skip sites, each owned with an expiry; untagged skips fail at run time"


def containers(tmp: Path) -> str:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    runs = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["containers"]["steps"])
    assert "test_testcontainers.py" in runs and ".[containers]" in runs, runs
    return "CI job 'containers' runs PostgreSQL and Kafka via testcontainers (unverified locally)"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("every adapter passes its port's conformance suite", conformance),
    ("mutation score 1.0 on the gate and the state machine", mutation),
    ("scenario matrix complete, non-heavy tier under 5 min", scenarios),
    ("no skip without an owner and an expiry", skips),
    ("testcontainers tier wired into CI", containers),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase13-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 13 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
