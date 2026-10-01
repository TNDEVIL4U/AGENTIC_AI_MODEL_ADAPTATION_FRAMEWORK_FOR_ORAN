"""No skip without an owner and an expiry (Hardening Phase 13).

Every skip, skipif, importorskip and xfail reason carries ``[owner=<who> expires=YYYY-MM-DD]``.
tests/conftest.py turns an untagged or expired skip into a failure when it happens, and
test_phase13_skip_policy scans the source so a skip that did not fire on this host is still
checked."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

TAG = re.compile(r"\[owner=(?P<owner>[\w.@-]+) expires=(?P<expires>\d{4}-\d{2}-\d{2})\]")

# The calls and marks that skip or expect a failure, and where each takes its reason.
SKIP_CALLS = {
    "pytest.skip": 0,
    "pytest.xfail": 0,
    "pytest.importorskip": None,  # reason= only (the first argument is the module)
    "pytest.mark.skip": 0,
    "pytest.mark.skipif": None,
    "pytest.mark.xfail": None,
}


def problem(reason: str | None, today: date) -> str | None:
    """Why ``reason`` breaks the policy, or None when it carries a live owner and expiry."""
    match = TAG.search(reason or "")
    if match is None:
        return "has no [owner=... expires=YYYY-MM-DD] tag"
    try:
        expires = date.fromisoformat(match["expires"])
    except ValueError:
        return f"has an invalid expiry {match['expires']}"
    if expires < today:
        return f"expired on {expires} (owner {match['owner']})"
    return None


def _text(node: ast.expr | None) -> str | None:
    """The literal text of a reason: a string, or the constant parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def skip_sites(path: Path) -> Iterator[tuple[int, str, str | None]]:
    """(line, call, reason text) for each skip-like call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name not in SKIP_CALLS:
            continue
        reason = next((k.value for k in node.keywords if k.arg in ("reason", "msg")), None)
        position = SKIP_CALLS[name]
        if reason is None and position is not None and len(node.args) > position:
            reason = node.args[position]
        yield node.lineno, name, _text(reason)
