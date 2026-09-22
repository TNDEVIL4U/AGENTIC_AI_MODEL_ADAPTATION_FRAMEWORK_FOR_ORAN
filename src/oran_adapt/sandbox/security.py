"""Member 3 - sandbox security: static AST analysis that rejects unsafe LLM-generated adaptation
code before it is ever executed. This is the first of two independent layers - the subprocess
runner's resource limits and restricted environment (sandbox.runner) are the second. Whitelist,
not blocklist: only the small set of imports and names adaptation code plausibly needs are
allowed; everything else - filesystem, network, process, and reflection/introspection escapes -
is rejected."""

from __future__ import annotations

import ast

from oran_adapt.core.errors import UnsafeCodeError

ALLOWED_IMPORT_ROOTS = frozenset(
    {"math", "json", "numpy", "pandas", "sklearn", "xgboost", "torch"}
)

# Builtins that read/execute arbitrary code, touch the filesystem or process, or dynamically
# resolve attributes in a way static analysis can't otherwise see through.
FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "exit",
        "quit",
        "memoryview",
        "help",
    }
)

# Attribute names that reach outside an object's declared interface into interpreter internals -
# the classic sandbox-escape route (obj.__class__.__bases__[0].__subclasses__() ...).
FORBIDDEN_ATTRS = frozenset(
    {
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__base__",
        "__class__",
        "__import__",
        "__loader__",
        "__code__",
        "__closure__",
        "__getattribute__",
        "__reduce__",
        "__reduce_ex__",
    }
)


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                self.violations.append(f"disallowed import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_IMPORT_ROOTS:
            self.violations.append(f"disallowed import: {node.module}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self.violations.append(f"disallowed name: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRS:
            self.violations.append(f"disallowed attribute access: {node.attr}")
        self.generic_visit(node)


def check_code_safety(code: str) -> None:
    """Raises UnsafeCodeError if ``code`` fails static analysis - unparsable, a disallowed
    import, a forbidden builtin, or a reflection-style attribute access. Never executes
    anything; this is a pure syntax-tree inspection."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise UnsafeCodeError(
            "generated code is not valid Python", cause=str(exc)
        ) from exc

    visitor = _SecurityVisitor()
    visitor.visit(tree)
    if visitor.violations:
        raise UnsafeCodeError(
            "generated code failed the security scan", violations=visitor.violations
        )
