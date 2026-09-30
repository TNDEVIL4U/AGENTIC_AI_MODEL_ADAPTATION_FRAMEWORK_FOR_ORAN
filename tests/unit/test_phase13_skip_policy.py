"""Hardening Phase 13: no skip without an owner and an expiry (tests/skip_policy.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import skip_policy
from conftest import _enforce_skip_policy

TESTS = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 30)


def test_every_skip_in_the_suite_has_a_live_owner_and_expiry() -> None:
    today = datetime.now(UTC).date()
    sites, broken = 0, []
    for path in sorted(TESTS.rglob("*.py")):
        for line, call, reason in skip_policy.skip_sites(path):
            sites += 1
            why = skip_policy.problem(reason, today)
            if why is not None:
                broken.append(f"{path.relative_to(TESTS)}:{line} {call} {why}")
    assert not broken, broken
    assert sites >= 8, "the scan found fewer skip sites than the suite has"


@pytest.mark.parametrize(("reason", "expected"), [
    ("needs docker [owner=ops expires=2027-01-01]", None),
    ("needs docker [owner=ops expires=2026-09-30]", None),  # the last day still counts
    ("needs docker [owner=ops expires=2026-09-29]", "expired on 2026-09-29 (owner ops)"),
    ("needs docker", "has no [owner=... expires=YYYY-MM-DD] tag"),
    ("needs docker [owner= expires=2027-01-01]", "has no [owner=... expires=YYYY-MM-DD] tag"),
    ("needs docker [owner=ops expires=2027-02-30]", "has an invalid expiry 2027-02-30"),
    (None, "has no [owner=... expires=YYYY-MM-DD] tag"),
])
def test_the_tag_needs_an_owner_and_a_date_not_yet_passed(reason, expected) -> None:
    assert skip_policy.problem(reason, TODAY) == expected


def test_the_scan_reads_each_kind_of_skip(tmp_path) -> None:
    source = tmp_path / "t.py"
    source.write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.skipif(True, reason='a [owner=x expires=2027-01-01]')\n"
        "@pytest.mark.xfail(reason='b')\n"
        "def test_a():\n"
        "    pytest.importorskip('jinja2', reason='c')\n"
        "    pytest.skip(f'd {1} [owner=y expires=2027-01-01]')\n"
        "    pytest.skip()\n"
        "    with pytest.raises(pytest.skip.Exception):\n"
        "        pass\n",
        encoding="utf-8",
    )
    found = [(call, reason) for _, call, reason in skip_policy.skip_sites(source)]
    assert found == [
        ("pytest.mark.skipif", "a [owner=x expires=2027-01-01]"),
        ("pytest.mark.xfail", "b"),
        ("pytest.importorskip", "c"),
        ("pytest.skip", "d  [owner=y expires=2027-01-01]"),
        ("pytest.skip", None),
    ]


def _report(**fields):
    return SimpleNamespace(skipped=True, outcome="skipped", **fields)


def test_an_untagged_skip_fails_when_it_happens() -> None:
    report = _report(longrepr=("t.py", 3, "Skipped: needs docker"))
    _enforce_skip_policy(report)
    assert report.outcome == "failed"
    assert "skip policy" in report.longrepr


def test_an_untagged_xfail_fails_when_it_happens() -> None:
    report = _report(longrepr=None, wasxfail="flaky")
    _enforce_skip_policy(report)
    assert report.outcome == "failed"


def test_a_tagged_skip_stays_skipped() -> None:
    report = _report(longrepr=("t.py", 3, "Skipped: needs docker [owner=ops expires=2999-01-01]"))
    _enforce_skip_policy(report)
    assert report.outcome == "skipped"
