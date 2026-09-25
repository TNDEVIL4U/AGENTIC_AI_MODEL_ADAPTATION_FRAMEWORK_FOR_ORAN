"""Run the release checks one after another (never in parallel - this also runs on a small
laptop) and write their output to reports/:

1. bandit     - static security scan of src/ (settings in pyproject.toml). Fails on any
                MEDIUM or HIGH severity finding.
2. pip-audit  - known vulnerabilities in the pinned runtime dependencies (requirements.lock,
                i.e. exactly what the Docker image installs). Fails on any advisory that is not
                listed, with a reason, in ACCEPTED_VULNS below.
3. mypy       - type check of src/ (settings in pyproject.toml). A ratchet that is now at
                zero: fails if the error count rises above MYPY_BASELINE.
4. SBOM       - CycloneDX JSON bill of materials of requirements.lock (reports/sbom.cdx.json).

Needs the security extra:  pip install -e ".[security]"
Usage:  python scripts/security_check.py [--skip-audit]   (the audit needs internet access)
Exit code 0 only when every check that ran passed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - fixed argument lists, no shell
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
LOCK = ROOT / "requirements.lock"

# Advisories reviewed and accepted, with the reason. Revisit when a fix is released.
ACCEPTED_VULNS = {
    "PYSEC-2026-3740": (
        "nltk (pulled in by evidently): path-sandbox bypass in NLTK's TransitionParser model "
        "file APIs. oran-adapt never imports nltk or calls those APIs; no fixed release yet."
    ),
}
MYPY_BASELINE = 0  # was 50 on 2026-09-24; all fixed the same day - keep it at 0


def _run(name: str, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"== {name}: {' '.join(cmd[2:])}", flush=True)
    return subprocess.run(  # nosec B603 - fixed argument list built above, no shell
        cmd, cwd=ROOT, capture_output=True, text=True, check=False
    )


def check_bandit() -> tuple[bool, str]:
    out = REPORTS / "bandit.json"
    proc = _run(
        "bandit",
        [
            sys.executable,
            "-m",
            "bandit",
            "-c",
            "pyproject.toml",
            "-r",
            "src/oran_adapt",
            "-f",
            "json",
            "-o",
            str(out),
            "-q",
        ],
    )
    if not out.exists():
        return False, f"bandit did not run: {proc.stderr.strip()[-300:]}"
    totals = json.loads(out.read_text(encoding="utf-8"))["metrics"]["_totals"]
    high, medium, low = (totals[f"SEVERITY.{s}"] for s in ("HIGH", "MEDIUM", "LOW"))
    return high == 0 and medium == 0, f"high={high} medium={medium} low={low}"


def check_audit() -> tuple[bool, str]:
    out = REPORTS / "pip-audit.json"
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(LOCK),
        "--no-deps",
        "--disable-pip",
        "--progress-spinner",
        "off",
        "-f",
        "json",
        "-o",
        str(out),
    ]
    for vuln in ACCEPTED_VULNS:
        cmd += ["--ignore-vuln", vuln]
    proc = _run("pip-audit", cmd)
    if not out.exists():
        return False, f"pip-audit did not run: {proc.stderr.strip()[-300:]}"
    deps = json.loads(out.read_text(encoding="utf-8"))["dependencies"]
    found = [f"{d['name']} {d['version']} {v['id']}" for d in deps for v in d.get("vulns", [])]
    summary = f"{len(deps)} packages, {len(found)} open advisories"
    summary += f", {len(ACCEPTED_VULNS)} accepted" if ACCEPTED_VULNS else ""
    return not found, summary + (": " + "; ".join(found) if found else "")


def check_mypy() -> tuple[bool, str]:
    proc = _run("mypy", [sys.executable, "-m", "mypy"])
    (REPORTS / "mypy.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    match = re.search(r"Found (\d+) errors?", proc.stdout)
    if match is None and "Success" not in proc.stdout:
        return False, f"mypy did not run: {(proc.stderr or proc.stdout).strip()[-300:]}"
    errors = int(match.group(1)) if match else 0
    return errors <= MYPY_BASELINE, f"{errors} errors (baseline {MYPY_BASELINE})"


def build_sbom() -> tuple[bool, str]:
    out = REPORTS / "sbom.cdx.json"
    proc = _run(
        "sbom",
        [
            sys.executable,
            "-m",
            "cyclonedx_py",
            "requirements",
            str(LOCK),
            "--output-format",
            "JSON",
            "--output-file",
            str(out),
        ],
    )
    if proc.returncode != 0 or not out.exists():
        return False, f"cyclonedx-py failed: {proc.stderr.strip()[-300:]}"
    components = json.loads(out.read_text(encoding="utf-8")).get("components", [])
    return True, f"{len(components)} components -> {out.relative_to(ROOT)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skip-audit", action="store_true", help="no network: skip pip-audit")
    args = parser.parse_args()
    REPORTS.mkdir(exist_ok=True)

    checks = [
        ("bandit", check_bandit),
        ("pip-audit", check_audit),
        ("mypy", check_mypy),
        ("sbom", build_sbom),
    ]
    results = {}
    for name, fn in checks:
        if name == "pip-audit" and args.skip_audit:
            results[name] = {"passed": None, "detail": "skipped (--skip-audit)"}
            continue
        passed, detail = fn()
        results[name] = {"passed": passed, "detail": detail}
        print(f"   {'PASS' if passed else 'FAIL'}  {detail}", flush=True)

    summary = {"checks": results, "accepted_vulnerabilities": ACCEPTED_VULNS}
    (REPORTS / "security-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if all(r["passed"] is not False for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
