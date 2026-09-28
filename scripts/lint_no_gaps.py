"""No-gaps lint: fail if production code carries unfinished work.

Scans every ``.py`` file under ``src/oran_adapt`` (or the paths given on the command line) for:

* ``TODO`` / ``FIXME`` / ``XXX`` / ``HACK`` markers in comments or strings;
* ``raise NotImplementedError`` anywhere outside an abstract method;
* bare ``except:`` clauses;
* stub bodies - a function whose body is only ``pass`` or ``...`` (after an optional docstring)
  unless it is an ``@abstractmethod``, an ``@overload``, or a method of a ``Protocol`` class.

Exit status is 0 when clean and 1 when any finding is reported, one ``path:line: message`` per
finding, so it can gate ``scripts/verify.sh`` and CI directly.
"""

from __future__ import annotations

import ast
import re
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "src" / "oran_adapt"
_MARKER = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _is_protocol(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
        if name == "Protocol":
            return True
    return False


def _is_stub_body(body: list[ast.stmt]) -> bool:
    stmts = body
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    if not stmts:
        return True  # docstring only
    if len(stmts) != 1:
        return False
    only = stmts[0]
    if isinstance(only, ast.Pass):
        return True
    return (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    )


def _raises_not_implemented(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []
        self._protocol_depth = 0
        self._abstract_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        protocol = _is_protocol(node)
        self._protocol_depth += protocol
        self.generic_visit(node)
        self._protocol_depth -= protocol

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = _decorator_names(node)
        exempt = bool(decorators & {"abstractmethod", "overload"}) or self._protocol_depth > 0
        if not exempt and _is_stub_body(node.body):
            self.findings.append((node.lineno, f"stub body in function {node.name!r}"))
        abstract = "abstractmethod" in decorators
        self._abstract_depth += abstract
        self.generic_visit(node)
        self._abstract_depth -= abstract

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Raise(self, node: ast.Raise) -> None:
        if _raises_not_implemented(node) and self._abstract_depth == 0:
            self.findings.append((node.lineno, "raise NotImplementedError"))
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.findings.append((node.lineno, "bare except"))
        self.generic_visit(node)


def _marker_findings(path: Path) -> Iterator[tuple[int, str]]:
    with path.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                match = _MARKER.search(tok.string)
                if match:
                    yield tok.start[0], f"{match.group(1)} marker"


def lint_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _Visitor()
    visitor.visit(tree)
    return sorted(visitor.findings + list(_marker_findings(path)))


def iter_python_files(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv] or [DEFAULT_ROOT]
    count = 0
    for path in iter_python_files(roots):
        for line, message in lint_file(path):
            print(f"{path}:{line}: {message}")
            count += 1
    if count:
        print(f"no-gaps lint: {count} finding(s)", file=sys.stderr)
        return 1
    print("no-gaps lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
