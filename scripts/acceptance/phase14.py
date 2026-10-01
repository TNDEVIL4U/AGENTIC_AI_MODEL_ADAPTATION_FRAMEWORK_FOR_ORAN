"""Hardening Phase 14 acceptance: integration, documentation and the clone-to-canary walkthrough.

Run by scripts/verify.sh 14 after lint, the import-boundary test and the scoped tests.

1. Every example config passes config lint, and the seven target stacks each have one.
2. The capability matrix and the OpenAPI document regenerate with no diff.
3. Every shipped drift mapper maps its sample payload, and the mapper tests passed.
4. The ``opa`` policy adapter passes the policy conformance suite, so no port has only one
   adapter.
5. Every Unknown-Stack default has an ADR, and the ADR index names only files that exist.
6. The integration, authoring, operations, migration and limitations docs exist, and every
   relative link (and heading anchor) in docs/ resolves.
7. The clone-to-canary walkthrough runs: an Alertmanager payload becomes a candidate serving a
   canary slice (in-process; the compose mode needs Docker and is unverified locally).

Exit status 0 means every check passed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
DOCS = ROOT / "docs"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gate import run_selection

PHASE_TESTS = TESTS / "test_phase14_integration.py"
TARGET_STACKS = ("mlflow-kserve", "sagemaker", "vertex", "seldon", "triton", "bentoml",
                 "airgapped-filesystem")
REQUIRED_DOCS = ("integration-guide.md", "adapter-authoring.md", "operations/README.md",
                 "migration-guide.md", "LIMITATIONS.md", "OPEN-QUESTIONS.md",
                 "capability-matrix.md", "adr/README.md", "api/openapi.json")
WALKTHROUGH_TIMEOUT_S = 240
LINK = re.compile(r"\]\(([^()\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _script(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


def example_configs(tmp: Path) -> str:
    from oran_adapt.core.config import lint_config_file

    examples = sorted((ROOT / "config" / "examples").glob("*.toml"))
    missing = [s for s in TARGET_STACKS if not (ROOT / "config/examples" / f"{s}.toml").is_file()]
    assert not missing, f"no example config for {missing}"
    problems = [p for path in examples for p in lint_config_file(str(path))]
    assert not problems, "config lint:\n      " + "\n      ".join(problems)
    return f"{len(examples)} example configs lint clean, covering all {len(TARGET_STACKS)} stacks"


def regenerated(tmp: Path) -> str:
    for script in ("capability_matrix.py", "openapi.py"):
        result = _script(f"scripts/{script}", "--check")
        assert result.returncode == 0, f"{script} --check: {result.stdout}{result.stderr}"
    spec = json.loads((DOCS / "api" / "openapi.json").read_text(encoding="utf-8"))
    return (f"capability-matrix.md and api/openapi.json are current "
            f"({len(spec['paths'])} paths)")


def mappers(tmp: Path) -> str:
    from oran_adapt.core.event_mapping import load_mapping, map_payload

    mapped = 0
    for path in sorted((ROOT / "config" / "mappers").glob("*.toml")):
        sample = path.parent / "samples" / f"{path.stem}.json"
        assert sample.is_file(), f"mapper {path.name} has no sample payload"
        payload = json.loads(sample.read_text(encoding="utf-8"))
        overrides = {"model_id": "acceptance"} if path.stem == "evidently" else None
        events = map_payload(load_mapping(str(path)), payload, max_events=100,
                             overrides=overrides)
        assert events, f"{path.name} mapped its sample to no events"
        mapped += len(events)
    how = run_selection(PHASE_TESTS, "maps or mapping or resolve or refused or api_ or cli_event")
    return f"every mapper maps its sample ({mapped} events); mapper tests {how}"


def opa_policy(tmp: Path) -> str:
    from oran_adapt import plugins

    names = sorted(plugins.adapters("policy"))
    assert names == ["opa", "static-rbac"], f"policy adapters: {names}"
    how = run_selection(PHASE_TESTS, "opa")
    return f"policy adapters {names}; opa conformance and fail-closed tests {how}"


def adrs(tmp: Path) -> str:
    index = (DOCS / "adr" / "README.md").read_text(encoding="utf-8")
    named = sorted(set(re.findall(r"\((\d{4}-[a-z0-9-]+\.md)\)", index)))
    files = sorted(p.name for p in (DOCS / "adr").glob("[0-9]*.md"))
    assert named == files, f"ADR index {named} != files {files}"
    how = run_selection(PHASE_TESTS, "adr")
    return f"{len(files)} ADRs, all indexed; every selector default has one ({how})"


def _anchors(markdown: Path) -> set[str]:
    anchors = set()
    for line in markdown.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            anchors.add(re.sub(r"[^\w\- ]", "", heading).replace(" ", "-"))
    return anchors


def docs(tmp: Path) -> str:
    missing = [d for d in REQUIRED_DOCS if not (DOCS / d).is_file()]
    assert not missing, f"missing docs: {missing}"
    broken, links = [], 0
    for page in sorted(DOCS.rglob("*.md")):
        text = FENCE.sub("", page.read_text(encoding="utf-8"))
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target):
                continue
            path, _, anchor = target.partition("#")
            resolved = (page.parent / path).resolve() if path else page
            links += 1
            if not resolved.exists():
                broken.append(f"{page.relative_to(ROOT)}: {target}")
            elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                broken.append(f"{page.relative_to(ROOT)}: {target} (no such heading)")
    assert not broken, "broken links:\n      " + "\n      ".join(broken)
    return f"{len(REQUIRED_DOCS)} required docs present; {links} relative links resolve"


def walkthrough(tmp: Path) -> str:
    result = _script("scripts/walkthrough.py", timeout=WALKTHROUGH_TIMEOUT_S)
    tail = "\n      ".join((result.stdout + result.stderr).strip().splitlines()[-8:])
    assert result.returncode == 0, f"walkthrough exit {result.returncode}:\n      {tail}"
    assert "Walkthrough complete" in result.stdout, tail
    return ("Alertmanager payload -> job -> DELIVERING -> CANARY at 5 %, duplicate refused "
            "(in-process; compose mode unverified locally)")


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("every example config passes config lint", example_configs),
    ("capability matrix and OpenAPI regenerate with no diff", regenerated),
    ("monitoring payloads map to DriftEvents", mappers),
    ("a second policy adapter (opa) passes conformance", opa_policy),
    ("an ADR for every Unknown-Stack default", adrs),
    ("docs present, links resolve", docs),
    ("clone-to-canary walkthrough", walkthrough),
]


def main() -> int:
    os.chdir(ROOT)
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase14-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 14 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
