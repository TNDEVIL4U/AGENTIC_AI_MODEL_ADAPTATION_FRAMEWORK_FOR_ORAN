"""Hardening Phase 13: mutation testing of the validation gate and the job state machine
(scripts/mutation.py with the kill tests in mutation_kills.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import mutation_kills
import pytest

from oran_adapt.core import state_machine
from oran_adapt.validation import gate

ROOT = Path(__file__).resolve().parents[2]


def _runner():
    spec = importlib.util.spec_from_file_location("oran_mutation", ROOT / "scripts" / "mutation.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


mutation = _runner()
KILLS = mutation.load_kills()


@pytest.mark.parametrize("name", [name for name, _ in KILLS])
def test_kill_test_passes_on_the_real_code(name) -> None:
    getattr(mutation_kills, name)()


@pytest.fixture(scope="module")
def results():
    return {target: mutation.run(target, KILLS) for target in sorted(mutation.TARGETS)}


@pytest.mark.parametrize("target", sorted(mutation.TARGETS))
def test_every_mutant_is_killed_or_documented_equivalent(results, target) -> None:
    mutants = results[target]
    assert len(mutants) > 30, "the mutator found too few sites; is it still visiting?"
    survivors = [f"{m.function} line {m.line}: {m.operator} [{m.source}]"
                 for m in mutants if m.killed_by is None and not m.equivalent]
    assert not survivors, survivors
    assert mutation.score(mutants) == 1.0


def test_equivalent_entries_still_match_a_survivor(results) -> None:
    seen = {(m.function, m.operator, m.source) for ms in results.values() for m in ms
            if m.killed_by is None}
    stale = [key for key in mutation.EQUIVALENT if key not in seen]
    assert not stale, f"no surviving mutant matches these EQUIVALENT entries: {stale}"


def test_every_operator_kind_is_exercised(results) -> None:
    operators = {m.operator.split(" ")[-1] for ms in results.values() for m in ms}
    for kind in ("LtE", "GtE", "NotEq", "NotIn", "Or", "And", "Sub", "Add", "Div", "False",
                 "USub", "Not", "max()", "min()"):
        assert any(op.endswith(kind) for op in operators), kind
    assert any("drop set element" in m.operator for m in results["state_machine"])


def test_the_originals_are_restored(results) -> None:
    mutation_kills.kill_state_machine_table_is_exact()
    mutation_kills.kill_golden_decisions()
    assert state_machine.allowed_next.__code__.co_filename.endswith("state_machine.py")
    assert gate.decide.__code__.co_filename.endswith("gate.py")


def test_weaker_kill_tests_let_mutants_survive() -> None:
    """The score means something: without the exact table, table mutants survive."""
    weak = [(name, fn) for name, fn in KILLS if name != "kill_state_machine_table_is_exact"
            and name != "kill_check_transition_agrees_with_the_table"]
    mutants = mutation.run("state_machine", weak)
    assert mutation.score(mutants) < 0.5


def test_the_timing_guardrail_is_the_only_code_left_unmutated() -> None:
    names = set(mutation.TARGETS["gate"][1]) | {k.rsplit(".", 1)[1] for k in mutation.NOT_MUTATED}
    defined = {n for n, v in vars(gate).items()
               if callable(v) and getattr(v, "__module__", None) == gate.__name__
               and not isinstance(v, type)}
    assert defined - names == {"serialized_bytes"}, defined - names
