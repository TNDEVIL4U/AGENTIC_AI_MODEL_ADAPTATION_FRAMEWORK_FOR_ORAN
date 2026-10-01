"""Hardening Phase 15 acceptance: the audit.

Run by scripts/verify.sh 15 (and by scripts/verify.sh all) after lint, the import-boundary
test and the scoped tests.

1. The audit documents exist, and every relative link in docs/ resolves.
2. The hardcoding audit passes (scripts/audit.py): every baseline literal accounted for, zero
   remaining, and no probe for one matches.
3. The findings matrix passes (scripts/audit.py): every row names modules, keys and tests that
   exist, and every named test passed in this gate's test run or was run by the audit.
4. The last C and D items are closed: the Phase 15 tests passed, and ``.env.example`` loads
   under the new validators.
5. The burn-down counters read 0 for A, B, C and D after Phase 15.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import phase14
from _gate import run_selection

PHASE_TESTS = ROOT / "tests" / "unit" / "test_phase15_audit.py"
AUDIT_DOCS = ("AUDIT-HARDCODING.md", "AUDIT-FINDINGS.md", "PRODUCTION-READINESS.md",
              "LIMITATIONS.md")
AUDIT_TIMEOUT_S = 600


def docs(tmp: Path) -> str:
    missing = [d for d in AUDIT_DOCS if not (DOCS / d).is_file()]
    assert not missing, f"missing docs: {missing}"
    return f"{len(AUDIT_DOCS)} audit docs present; " + phase14.docs(tmp)


def _audit(scan: str) -> str:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "scripts/audit.py", "--only", scan], cwd=ROOT,
                            capture_output=True, text=True, timeout=AUDIT_TIMEOUT_S, env=env,
                            check=False)
    lines = [line for line in result.stdout.splitlines()
             if line.startswith(("  hardcoding", "  findings", "PASS", "FAIL", "      "))]
    assert result.returncode == 0, "\n      ".join(lines) or result.stderr[-2000:]
    return next((line.strip() for line in lines if line.startswith("  ")), "passed")


def hardcoding(tmp: Path) -> str:
    return _audit("hardcoding")


def findings(tmp: Path) -> str:
    return _audit("findings")


def closures(tmp: Path) -> str:
    from oran_adapt.core.config import Settings

    settings = Settings(_env_file=str(ROOT / ".env.example"))
    assert settings.sandbox_backend == "subprocess", settings.sandbox_backend
    how = run_selection(PHASE_TESTS, "sandbox or kafka or llm or model_id or mlflow or default")
    return f".env.example loads; C4-C6, C10, C11 and D7 tests {how}"


def burn_down(tmp: Path) -> str:
    lines = (DOCS / "hardcoding-inventory.md").read_text(encoding="utf-8").splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("| Category | Count at"))
    assert lines[i].endswith("| Open after Phase 15 |"), lines[i]
    last = [lines[k].rstrip(" |").rsplit("|", 1)[-1].strip() for k in range(i + 2, i + 6)]
    assert last == ["0", "0", "0", "0"], f"A-D open after Phase 15: {last}"
    return "A 0, B 0, C 0, D 0"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("audit docs present, links resolve", docs),
    ("hardcoding audit: zero remaining", hardcoding),
    ("findings matrix: every named test ran and passed", findings),
    ("the last C and D items are closed", closures),
    ("burn-down counters at zero", burn_down),
]


def main() -> int:
    os.chdir(ROOT)
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase15-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 15 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
