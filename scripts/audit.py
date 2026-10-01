"""The Phase 15 audit scans: the hardcoding audit and the findings matrix.

    python scripts/audit.py [--junit REPORT] [--only hardcoding|findings]

1. Hardcoding (docs/AUDIT-HARDCODING.md): the tables hold each of A1-A24, B1-B7, C1-C14 and
   D1-D8 exactly once, every status is ``closed`` or ``kept``, "Remaining" is 0, every path in
   the "Where" column exists, every key is a ``Settings`` field, a compose variable listed in
   ``.env.example`` or a ``Dockerfile`` build argument, and no probe for a baseline literal
   matches where it must not.
2. Findings (docs/AUDIT-FINDINGS.md): findings 1-10 each appear once, every module exists, every
   key is a ``Settings`` field, and every named test exists and passed. A test passed when
   the JUnit report holds it (every case of a parametrised test) with no failure, error or
   skip. The report is ``--junit``, else every report in ``ORAN_GATE_JUNIT``'s directory newer
   than ``ORAN_GATE_STARTED`` (scripts/verify.sh: the scoped and smoke runs). Tests no report
   holds are run here (at most 2 workers), so every named test has run.

Exit status 0 means every scan passed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIG_PY = "src/oran_adapt/core/config.py"
EXPECTED_IDS = ([f"A{i}" for i in range(1, 25)] + [f"B{i}" for i in range(1, 8)]
                + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 9)])
TICK = re.compile(r"`([^`]+)`")
KEY = re.compile(r"[A-Z][A-Z0-9_]*")
TEST = re.compile(r"(tests/[\w/]+\.py)::(\w+)")

# Baseline literals that must not come back: (files, pattern, files allowed to hold it).
SRC_PROBES = (
    r"claude-sonnet-5", r"gemini-3\.6-flash", r"oran-adapt-sandbox:latest",
    r"_KILL_GRACE_S\s*=", r"pids-limit.{0,6}128", r"size=64m", r"_MANIFEST_MAX_BYTES\s*=",
    r"_MAX_TX_IDS\s*=", r"_MEMORY_COPIES\s*=", r"DEFAULT_ARTIFACT_MAX_BYTES", r"\b_CHUNK\s*=",
    r"max_tokens\s*=\s*2048", r"\.limit\(10\)", r"idle_sleep_s\s*=\s*1\.0",
    r"min_psi_rows\s*=\s*30", r"\bepochs\s*=\s*(5|300)\b", r"\blr\s*=\s*1e-2",
    r"live_alias\s*=\s*\"live\"", r"\"X-API-Key\"", r"\"X-Correlation-ID\"",
    r"\"artifact\.sha256\"", r"\"oran\.status\"", r"\"kpi_sample/1\"",
    r"table\s*=\s*\"kpi_sample\"", r"oran\.public\.kpi_sample", r"\"oran-adapt-cdc\"",
)
FILE_PROBES = {
    "docker-compose.yml": (r"oran-adapt-(api|worker|migrator|mlflow):[0-9]",
                           r"pg_isready -U oran", r"connectors/oran-kpi-sample",
                           r"GROUP_ID: oran-connect", r"127\.0\.0\.1:[0-9]+:",
                           r"POSTGRES_(USER|DB): oran"),
    "deploy/debezium/kpi-connector.json": (r"\"oran_adapt\"", r"\"oran\"",
                                           r"public\.kpi_sample", r"oran_kpi_sample"),
    "docker/mlflow/Dockerfile": (r"mlflow==", r"psycopg\S*==",),
}


def _rows(text: str, first: str) -> list[list[str]]:
    """Table rows whose first cell matches ``first``, split into stripped cells."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if re.fullmatch(first, cells[0]):
            rows.append(cells)
    return rows


def _settings_keys() -> set[str]:
    from oran_adapt.core.config import Settings

    return {name.upper() for name in Settings.model_fields}


def _known_keys() -> set[str]:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", env, re.MULTILINE))
    args = set(re.findall(r"^ARG ([A-Z][A-Z0-9_]*)", (ROOT / "Dockerfile").read_text(
        encoding="utf-8"), re.MULTILINE))
    return _settings_keys() | compose | args


def _paths(cell: str) -> list[str]:
    return [t for t in TICK.findall(cell)
            if "/" in t and " " not in t and not t.startswith(("/", "http", "$"))
            and "..." not in t and "<" not in t]


def hardcoding() -> list[str]:
    text = (DOCS / "AUDIT-HARDCODING.md").read_text(encoding="utf-8")
    problems = []
    rows = _rows(text, r"[A-D]\d+")
    ids = [r[0] for r in rows]
    if sorted(ids) != sorted(EXPECTED_IDS):
        missing = sorted(set(EXPECTED_IDS) - set(ids))
        extra = sorted({i for i in ids if ids.count(i) > 1 or i not in EXPECTED_IDS})
        problems.append(f"ids: missing {missing}, duplicated or unknown {extra}")
    if not re.search(r"\*\*Remaining: 0\.\*\*", text):
        problems.append("the 'Remaining: 0.' line is missing")
    known = _known_keys()
    for row in rows:
        if len(row) != 5:
            problems.append(f"{row[0]}: expected 5 cells, got {len(row)}")
            continue
        ident, _, where, keys, status = row
        if not status.startswith(("closed", "kept")):
            problems.append(f"{ident}: status {status!r} is neither closed nor kept")
        if status.startswith("kept") and ":" not in status:
            problems.append(f"{ident}: a kept literal needs its reason")
        for path in _paths(where):
            if not (ROOT / path.rstrip("/")).exists():
                problems.append(f"{ident}: {path} does not exist")
        for key in TICK.findall(keys):
            if not KEY.fullmatch(key) or key not in known:
                problems.append(f"{ident}: {key} is not a Settings field, compose variable "
                                "or build argument")
        if not TICK.findall(keys) and not status.startswith("kept"):
            problems.append(f"{ident}: a closed literal names no key")
    probes = [re.compile(p) for p in SRC_PROBES]
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == CONFIG_PY:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for probe in probes:
                if probe.search(line):
                    problems.append(f"{rel}:{number}: baseline literal /{probe.pattern}/")
    for rel, patterns in FILE_PROBES.items():
        content = (ROOT / rel).read_text(encoding="utf-8")
        for pattern in patterns:
            if re.search(pattern, content):
                problems.append(f"{rel}: baseline literal /{pattern}/")
    print(f"  hardcoding: {len(rows)} literals, {sum(r[-1].startswith('kept') for r in rows)} "
          f"kept with a reason, {len(SRC_PROBES)} source probes over src/, "
          f"{sum(map(len, FILE_PROBES.values()))} infrastructure probes", flush=True)
    return problems


def _reports(explicit: str | None) -> list[Path]:
    """The explicit report, else every report this gate wrote beside ``ORAN_GATE_JUNIT``."""
    if explicit:
        return [Path(explicit)]
    report, started = os.environ.get("ORAN_GATE_JUNIT"), os.environ.get("ORAN_GATE_STARTED")
    if not (report and started):
        return []
    return sorted(p for p in Path(report).parent.glob("*.xml")
                  if p.stat().st_mtime >= float(started))


def _outcomes(report: Path, tests: list[tuple[str, str]]) -> dict[tuple[str, str], list[bool]]:
    wanted = {(Path(f).stem, name) for f, name in tests}
    by_stem = {Path(f).stem: f for f, _ in tests}
    found: dict[tuple[str, str], list[bool]] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        stem = case.get("classname", "").rsplit(".", 1)[-1]
        name = case.get("name", "").split("[", 1)[0]
        if (stem, name) in wanted:
            ok = not any(child.tag in {"failure", "error", "skipped"} for child in case)
            found.setdefault((by_stem[stem], name), []).append(ok)
    return found


def _run(tests: list[tuple[str, str]]) -> dict[tuple[str, str], list[bool]]:
    with tempfile.TemporaryDirectory(prefix="oran-audit-", ignore_cleanup_errors=True) as tmp:
        report = Path(tmp) / "audit.xml"
        ids = [f"{f}::{name}" for f, name in tests]
        workers = min(int(os.environ.get("VERIFY_WORKERS", "2")), 2)  # CLAUDE.md cap
        print(f"  running {len(ids)} named tests the reports do not hold", flush=True)
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "-W", "ignore", "-n", str(workers), "--junitxml", str(report), *ids],
                       cwd=ROOT, check=False)
        return _outcomes(report, tests) if report.is_file() else {}


def findings(junit: str | None) -> list[str]:
    text = (DOCS / "AUDIT-FINDINGS.md").read_text(encoding="utf-8")
    problems = []
    rows = _rows(text, r"\d+")
    numbers = sorted(int(r[0]) for r in rows)
    if numbers != list(range(1, 11)):
        problems.append(f"findings {numbers}, expected 1-10 once each")
    settings = _settings_keys()
    tests: list[tuple[str, str]] = []
    for row in rows:
        if len(row) != 6:
            problems.append(f"finding {row[0]}: expected 6 cells, got {len(row)}")
            continue
        number, _, _, modules, keys, named = row
        if not _paths(modules):
            problems.append(f"finding {number}: no module")
        for path in _paths(modules):
            if not (ROOT / path.rstrip("/")).exists():
                problems.append(f"finding {number}: {path} does not exist")
        if not TICK.findall(keys):
            problems.append(f"finding {number}: no config key")
        for key in TICK.findall(keys):
            if key not in settings:
                problems.append(f"finding {number}: {key} is not a Settings field")
        row_tests = TEST.findall(named)
        if not row_tests:
            problems.append(f"finding {number}: no test")
        for file, name in row_tests:
            path = ROOT / file
            source = path.read_text(encoding="utf-8") if path.is_file() else ""
            if not re.search(rf"^def {name}\(", source, re.MULTILINE):
                problems.append(f"finding {number}: {file}::{name} does not exist")
            elif re.search(rf"^@pytest\.mark\.heavy.*\n(@.*\n)*def {name}\(", source,
                           re.MULTILINE):
                problems.append(f"finding {number}: {file}::{name} is heavy; the gate never "
                                "runs it, so name a gate-tier test")
            else:
                tests.append((file, name))
    outcomes: dict[tuple[str, str], list[bool]] = {}
    for report in _reports(junit):
        for test, cases in _outcomes(report, tests).items():
            outcomes.setdefault(test, []).extend(cases)
    missing = [t for t in tests if t not in outcomes]
    if missing:
        outcomes.update(_run(missing))
    for file, name in tests:
        results = outcomes.get((file, name))
        if not results:
            problems.append(f"{file}::{name} did not run")
        elif not all(results):
            problems.append(f"{file}::{name}: {results.count(False)} of {len(results)} cases "
                            "did not pass")
    ran = sum(len(outcomes.get(t, [])) for t in tests)
    print(f"  findings: {len(rows)} rows, {len(tests)} named tests ({ran} cases); "
          f"{len(tests) - len(missing)} read from the gate's report, {len(missing)} run here",
          flush=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", help="a JUnit report of a test run that holds the tests")
    parser.add_argument("--only", choices=("hardcoding", "findings"))
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    failed = 0
    scans = [("hardcoding", hardcoding), ("findings", lambda: findings(args.junit))]
    for name, scan in scans:
        if args.only and args.only != name:
            continue
        problems = scan()
        if problems:
            failed += 1
            print(f"FAIL  audit {name}\n      " + "\n      ".join(problems), flush=True)
        else:
            print(f"PASS  audit {name}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
