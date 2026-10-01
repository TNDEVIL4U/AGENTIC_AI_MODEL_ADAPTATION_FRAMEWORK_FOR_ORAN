"""Mutation testing for the validation gate and the job state machine (Hardening Phase 13).

    python scripts/mutation.py                      # both targets, fail below --min-score
    python scripts/mutation.py --target state_machine --json out.json
    python scripts/mutation.py --write-golden       # after an intended change to the gate

No mutation tool is installed (mutmut / cosmic-ray are not dependencies), so this is a small,
in-process one. For each target function it parses the module source, applies one operator at
one site (a comparison flipped, and/or swapped, a ``not`` dropped, an arithmetic operator
swapped, a constant nudged, min/max swapped, a set element dropped), compiles the mutated
function with the module's globals and swaps its ``__code__`` in place, so every caller,
including modules that imported the function by name, runs the mutant. A module-level table
(the state machine's ``_ALLOWED``) is mutated by rebinding the name. After each mutant the
original is restored.

A mutant is killed when any kill test (``tests/unit/mutation_kills.py``: plain functions named
``kill_*``, no fixtures) raises. The score is killed / total; survivors are listed with their
line so each one is either killed by a new kill test or recognised as equivalent. The
wall-clock latency guardrail (``_median_ms``, ``_latency``) is not mutated: its result depends
on timing, so no deterministic test can kill its mutants.
"""

from __future__ import annotations
import __future__

import argparse
import ast
import copy
import importlib
import inspect
import json
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FunctionType, ModuleType

ROOT = Path(__file__).resolve().parents[1]
KILLS_DIR = ROOT / "tests" / "unit"

TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "state_machine": ("oran_adapt.core.state_machine",
                      ("_ALLOWED", "allowed_next", "check_transition")),
    "gate": ("oran_adapt.validation.gate",
             ("_mean", "_root_mean", "_f1", "_rows", "_point", "_bootstrap", "_slices", "_ece",
              "_positive", "_calibration", "_size", "_metric_guards", "decide")),
}
NOT_MUTATED = {
    "oran_adapt.validation.gate._median_ms": "measures wall-clock time",
    "oran_adapt.validation.gate._latency": "compares wall-clock timings",
}

# Mutants that cannot change behaviour, (function, operator, source line) -> why. They are
# reported but left out of the score; test_phase13_mutation fails on an entry that no longer
# matches a surviving mutant, so the list cannot go stale.
EQUIVALENT: dict[tuple[str, str, str], str] = {
    ("_f1", "Gt -> GtE",
     "return np.where(denominator > 0, 2 * tp / np.where(denominator > 0, denominator, 1), 0.0)"):
        "denominator is a sum of counts, never negative; at 0 the other where() already picks 0",
    ("_f1", "1 -> 2",
     "return np.where(denominator > 0, 2 * tp / np.where(denominator > 0, denominator, 1), 0.0)"):
        "the divisor substituted for a zero denominator is discarded by the outer where()",
    ("_ece", "Sub -> Add", "which = np.clip(np.digitize(prob, edges[1:-1]), 0, bins - 1)"):
        "digitize over the bins - 1 inner edges never exceeds bins - 1, so the upper clip is "
        "a guard that cannot fire",
    ("allowed_next", "drop tuple element S.RECEIVED",
     "if status not in (S.RECEIVED, S.QUEUED):"):
        "the retry target QUEUED is already an allowed move from RECEIVED",
}

_SWAP_CMP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
_SWAP_BIN: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult, ast.Pow: ast.Mult,
}
_SWAP_CALL = {"min": "max", "max": "min", "any": "all", "all": "any"}


@dataclass
class Mutant:
    target: str
    function: str
    line: int
    operator: str
    source: str
    killed_by: str | None = None

    @property
    def equivalent(self) -> str | None:
        return EQUIVALENT.get((self.function, self.operator, self.source))


class _Mutator(ast.NodeTransformer):
    """Applies the ``index``-th mutation of a tree (index -1: none, just count the sites)."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.count = 0
        self.applied: tuple[int, str] | None = None

    def _site(self, node: ast.AST, label: str) -> bool:
        hit = self.count == self.index
        self.count += 1
        if hit:
            self.applied = (getattr(node, "lineno", 0), label)
        return hit

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        # Docstrings and bare string expressions carry no behaviour.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node
        return self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        return node  # message text: mutating it tests wording, not behaviour

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            swap = _SWAP_CMP.get(type(op))
            if swap is not None and self._site(node, f"{type(op).__name__} -> {swap.__name__}"):
                node.ops[i] = swap()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        swap = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._site(node, f"{type(node.op).__name__} -> {swap.__name__}"):
            node.op = swap()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, (ast.Not, ast.USub)) and self._site(
                node, f"drop {type(node.op).__name__}"):
            return node.operand
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        swap = _SWAP_BIN.get(type(node.op))
        if swap is not None and self._site(node, f"{type(node.op).__name__} -> {swap.__name__}"):
            node.op = swap()
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if isinstance(value, bool):
            if self._site(node, f"{value} -> {not value}"):
                return ast.copy_location(ast.Constant(not value), node)
        elif isinstance(value, (int, float)) and self._site(node, f"{value!r} -> {value + 1!r}"):
            return ast.copy_location(ast.Constant(value + 1), node)
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in _SWAP_CALL:
            swap = _SWAP_CALL[node.func.id]
            if self._site(node, f"{node.func.id}() -> {swap}()"):
                node.func = ast.copy_location(ast.Name(swap, ast.Load()), node.func)
        return node

    def visit_Set(self, node: ast.Set) -> ast.AST:
        self.generic_visit(node)
        if len(node.elts) > 1:
            for i, elt in enumerate(list(node.elts)):
                if self._site(elt, f"drop set element {ast.unparse(elt)}"):
                    del node.elts[i]
                    break
        return node

    def visit_Tuple(self, node: ast.Tuple) -> ast.AST:
        self.generic_visit(node)
        # Membership tuples (`x in (a, b)`) are sets in all but syntax.
        if getattr(node, "_membership", False) and len(node.elts) > 1:
            for i, elt in enumerate(list(node.elts)):
                if self._site(elt, f"drop tuple element {ast.unparse(elt)}"):
                    del node.elts[i]
                    break
        return node


def _mark_membership(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Tuple):
                    right._membership = True  # type: ignore[attr-defined]


def _definitions(module: ModuleType, names: tuple[str, ...]) -> list[tuple[str, ast.stmt]]:
    tree = ast.parse(inspect.getsource(module))
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in names:
            found[node.target.id] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            found[node.targets[0].id] = node
    missing = set(names) - set(found)
    if missing:
        raise SystemExit(f"{module.__name__}: no definition of {sorted(missing)}")
    return [(name, found[name]) for name in names]


def _sites(node: ast.stmt) -> int:
    probe = copy.deepcopy(node)
    _mark_membership(probe)
    counter = _Mutator(-1)
    counter.visit(probe)
    return counter.count


def _mutate(node: ast.stmt, index: int) -> tuple[ast.stmt, int, str]:
    tree = copy.deepcopy(node)
    _mark_membership(tree)
    mutator = _Mutator(index)
    mutated = mutator.visit(tree)
    assert mutator.applied is not None, index
    ast.fix_missing_locations(mutated)
    return mutated, *mutator.applied


_FLAGS = __future__.annotations.compiler_flag


@contextmanager
def _applied(module: ModuleType, name: str, node: ast.stmt) -> Iterator[None]:
    code = compile(ast.Module(body=[node], type_ignores=[]), inspect.getfile(module), "exec",
                   flags=_FLAGS, dont_inherit=True)
    scope: dict[str, object] = {}
    exec(code, module.__dict__, scope)  # noqa: S102  # nosec B102 - our own source, mutated
    original = getattr(module, name)
    if isinstance(original, FunctionType):
        saved = original.__code__
        original.__code__ = scope[name].__code__  # type: ignore[attr-defined]
        try:
            yield
        finally:
            original.__code__ = saved
    else:
        setattr(module, name, scope[name])
        try:
            yield
        finally:
            setattr(module, name, original)


def load_kills() -> list[tuple[str, Callable[[], None]]]:
    if str(KILLS_DIR) not in sys.path:
        sys.path.insert(0, str(KILLS_DIR))
    kills = importlib.import_module("mutation_kills")
    return [(name, fn) for name, fn in inspect.getmembers(kills, inspect.isfunction)
            if name.startswith("kill_") and fn.__module__ == kills.__name__]


def _first_failure(kills: list[tuple[str, Callable[[], None]]]) -> str | None:
    for name, kill in kills:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                kill()
        except Exception:  # noqa: BLE001 - any failure of a kill test kills the mutant
            return name
    return None


def run(target: str, kills: list[tuple[str, Callable[[], None]]] | None = None) -> list[Mutant]:
    """Every mutant of ``target`` (a TARGETS key), each marked with the kill test that
    killed it (None: it survived)."""
    kills = kills if kills is not None else load_kills()
    failing = _first_failure(kills)
    if failing is not None:
        raise SystemExit(f"kill test {failing} fails on the unmutated code")
    module_name, names = TARGETS[target]
    module = importlib.import_module(module_name)
    lines = inspect.getsource(module).splitlines()
    out = []
    for name, node in _definitions(module, names):
        for index in range(_sites(node)):
            mutated, line, operator = _mutate(node, index)
            with _applied(module, name, mutated):
                killer = _first_failure(kills)
            out.append(Mutant(target, name, line, operator, lines[line - 1].strip(), killer))
    return out


def score(mutants: list[Mutant]) -> float:
    """Killed / (mutants that can change behaviour): equivalent survivors are left out."""
    counted = [m for m in mutants if not (m.killed_by is None and m.equivalent)]
    return sum(m.killed_by is not None for m in counted) / len(counted) if counted else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=sorted(TARGETS), action="append")
    parser.add_argument("--min-score", type=float, default=1.0)
    parser.add_argument("--json", type=Path, help="write every mutant and its outcome here")
    parser.add_argument("--write-golden", action="store_true",
                        help="regenerate the gate's golden decisions and exit")
    args = parser.parse_args(argv)
    if args.write_golden:
        load_kills()
        importlib.import_module("mutation_kills").write_golden()
        return 0
    kills = load_kills()
    ok = True
    report = {}
    for target in args.target or sorted(TARGETS):
        started = time.monotonic()
        mutants = run(target, kills)
        value = score(mutants)
        survivors = [m for m in mutants if m.killed_by is None]
        equivalent = [m for m in survivors if m.equivalent]
        print(f"{target}: {len(mutants)} mutants, {len(mutants) - len(survivors)} killed, "
              f"{len(equivalent)} equivalent, score {value:.3f} (min {args.min_score}) "
              f"in {time.monotonic() - started:.1f}s")
        for m in survivors:
            why = f" (equivalent: {m.equivalent})" if m.equivalent else ""
            print(f"  survived: {m.function} line {m.line}: {m.operator}{why}")
        ok = ok and value >= args.min_score
        report[target] = {"score": value, "mutants": [
            {**asdict(m), "equivalent": m.equivalent} for m in mutants]}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
