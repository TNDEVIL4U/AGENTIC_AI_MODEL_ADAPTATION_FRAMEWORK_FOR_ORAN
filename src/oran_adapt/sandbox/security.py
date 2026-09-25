"""Member 3 - sandbox security: static AST analysis that rejects unsafe LLM-generated adaptation
code before it is ever executed. This is the first of two independent layers - the subprocess
runner's resource limits and restricted environment (sandbox.runner) are the second. Whitelist,
not blocklist: only the small set of imports and names adaptation code plausibly needs are
allowed; everything else - filesystem, network, process, and reflection/introspection escapes -
is rejected.

Static analysis of Python cannot be complete: this scanner narrows what hostile code can reach,
but the process boundary (sandbox.runner) is what contains it. Production should use the Docker
backend, whose container has no network, a read-only root filesystem and enforced limits."""

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
        "__builtins__",
        "__loader__",
        "__spec__",
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
        "__dict__",
        "__getattr__",
        "__setattr__",
        "__self__",
        "__func__",
        "__spec__",
        # Frame and code objects of generators, coroutines and tracebacks lead to f_globals /
        # f_builtins, i.e. back to the unrestricted builtins.
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "tb_frame",
        "tb_next",
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_back",
        "f_code",
        # "{0.__class__.__init__.__globals__}".format(obj) walks attributes named inside a
        # string, where the syntax-tree scan cannot see them; f-strings are parsed and checked.
        "format",
        "format_map",
        # Allowed packages re-export dangerous modules as attributes, e.g.
        # pandas.io.common.os.system(...) - so the module names themselves are refused as
        # attributes, whatever object they are reached through.
        "os",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "builtins",
        "importlib",
        "ctypes",
        "ctypeslib",
        "pickle",
        "marshal",
        "multiprocessing",
        "threading",
        "signal",
        "pathlib",
        "io",
        "hub",
        "cpp_extension",
        "system",
        "popen",
        # Filesystem and deserialization entry points of numpy / pandas / torch / xgboost.
        # Loading (np.load, torch.load, joblib via sklearn) can unpickle attacker bytes; the
        # writers let code plant files for a later stage to trip over.
        "load",
        "save",
        "savez",
        "savez_compressed",
        "fromfile",
        "tofile",
        "memmap",
        "loadtxt",
        "savetxt",
        "genfromtxt",
        "load_library",
        "load_model",
        "save_model",
        "compile",
        "to_pickle",
        "to_csv",
        "to_json",
        "to_parquet",
        "to_feather",
        "to_hdf",
        "to_excel",
        "to_sql",
        "to_stata",
        "to_orc",
        "to_html",
        "to_xml",
        "to_latex",
        "to_markdown",
        "to_clipboard",
    }
)

# pandas.read_csv / read_pickle / read_sql / ... all read from the filesystem or network;
# sklearn.datasets.fetch_openml / fetch_california_housing / ... download from the internet.
FORBIDDEN_ATTR_PREFIXES = ("read_", "fetch_")

# eval/query with an argument evaluate a string expression the scanner cannot see into
# (pandas.eval, DataFrame.eval, DataFrame.query). torch's no-argument ``module.eval()`` is fine.
FORBIDDEN_CALLS_WITH_ARGS = frozenset({"eval", "query"})


def _forbidden_attr(name: str) -> bool:
    return name in FORBIDDEN_ATTRS or name.startswith(FORBIDDEN_ATTR_PREFIXES)


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def _check_module(self, module: str) -> None:
        parts = module.split(".")
        if parts[0] not in ALLOWED_IMPORT_ROOTS:
            self.violations.append(f"disallowed import: {module}")
        elif any(_forbidden_attr(p) for p in parts[1:]):
            # e.g. pandas.io.common, numpy.ctypeslib, torch.hub
            self.violations.append(f"disallowed import: {module}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.violations.append("disallowed relative import")
        self._check_module(node.module or "")
        for alias in node.names:
            # `from numpy import load` reaches the same function as `numpy.load`.
            if alias.name == "*" or _forbidden_attr(alias.name) or alias.name in FORBIDDEN_NAMES:
                self.violations.append(f"disallowed import: {node.module}.{alias.name}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self.violations.append(f"disallowed name: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _forbidden_attr(node.attr):
            self.violations.append(f"disallowed attribute access: {node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in FORBIDDEN_CALLS_WITH_ARGS
            and (node.args or node.keywords)
        ):
            self.violations.append(f"disallowed string-evaluating call: {func.attr}(...)")
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
