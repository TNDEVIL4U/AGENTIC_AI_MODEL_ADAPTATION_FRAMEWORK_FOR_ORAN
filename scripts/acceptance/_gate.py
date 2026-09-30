"""Shared helper for the phase acceptance scripts: prove that a selection of tests passes.

Inside ``scripts/verify.sh`` the scoped test step has just run the phase's test files and
written a JUnit report (``ORAN_GATE_JUNIT``, newer than ``ORAN_GATE_STARTED``). A selection is
then checked against that report instead of being run a second time: the ``-k`` expression is
evaluated with pytest's own expression parser against each recorded test name, and the check
passes only if at least one test matches and every match passed (a skip is not a pass).

Run on its own (no fresh report for the file), the selection is run with pytest as before.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path


def _recorded(target: Path) -> dict[str, bool] | None:
    report = os.environ.get("ORAN_GATE_JUNIT")
    started = os.environ.get("ORAN_GATE_STARTED")
    if not report or not started:
        return None
    path = Path(report)
    if not path.is_file() or path.stat().st_mtime < float(started):
        return None
    module = target.stem
    outcomes: dict[str, bool] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        if case.get("classname", "").rsplit(".", 1)[-1] != module:
            continue
        failed = any(child.tag in {"failure", "error", "skipped"} for child in case)
        outcomes[case.get("name", "")] = not failed
    return outcomes or None


def _matches(selection: str, name: str) -> bool:
    from _pytest.mark.expression import Expression

    lowered = name.lower()

    def matcher(word: str, /, **_: object) -> bool:
        return word.lower() in lowered

    return Expression.compile(selection).evaluate(matcher)


def run_selection(target: Path, selection: str, *extra: str) -> str:
    """Assert that the tests in ``target`` selected by ``-k selection`` pass; say how."""
    recorded = _recorded(target) if not extra else None
    if recorded is not None:
        chosen = {name: ok for name, ok in recorded.items() if _matches(selection, name)}
        assert chosen, f"no test in {target.name} matches -k {selection!r}"
        failed = sorted(name for name, ok in chosen.items() if not ok)
        assert not failed, f"-k {selection!r}: did not pass in the gate run: {failed}"
        print(f"{len(chosen)} passed (recorded by this gate's scoped test run)", flush=True)
        return "recorded"
    import pytest

    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", *extra, str(target),
                        "-k", selection])
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(getattr(handler, "stream", None), "closed", False):
            root.removeHandler(handler)
    assert code == 0, f"pytest -k {selection!r} failed with exit code {code}"
    return "run"
