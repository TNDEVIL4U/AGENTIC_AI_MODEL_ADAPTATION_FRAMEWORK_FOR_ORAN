"""Hardening Phase 11 acceptance: packaging and deployment.

Run by scripts/verify.sh 11 after lint, the import-boundary test and the scoped tests.

1. Images: one Dockerfile, targets api (default), worker and migrator, from a base pinned by
   digest, non-root, each with its health check and SIGTERM handling; mlflow and sandbox
   pinned by digest too.
2. Compose: every pulled image pinned by digest; a one-shot `migrate` service that everything
   reading the schema waits for; .env optional, so a fresh clone needs only POSTGRES_PASSWORD;
   the smoke script checks the schema state and the worker's liveness.
3. Helm: every example values file validates against values.schema.json; every values path a
   template reads is declared; API + HPA + PDB, workers per queue class (a GPU pool), the
   migration hook Job, per-port ConfigMaps, ExternalSecret, Ingress + TLS, NetworkPolicy,
   ServiceMonitor, PrometheusRule (every alert names an existing runbook); a kustomize base and
   overlays.
4. No manifest holds an environment-specific literal, and every setting a manifest passes is a
   key the application reads.
5. Probes and graceful shutdown: the worker liveness file, `oran-adapt worker health`, the
   health window validator, `db status` / `db wait` (behind, at_head, ahead, unreachable).
6. Expand/contract: no migration's upgrade() contracts the schema.
7. What the laptop cannot run is wired into CI: helm lint + template per example values file,
   kubeconform, kustomize build, image SBOMs, the compose smoke, and the scheduled kind
   install/upgrade/rollback. Those are unverified locally.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gate import run_selection

PHASE11 = TESTS / "test_phase11_packaging.py"


def _run(selection: str) -> None:
    run_selection(PHASE11, selection)


def images(tmp: Path) -> str:
    _run("dockerfile_builds or other_image_base")
    return "api (default), worker, migrator; base and mlflow/sandbox pinned by digest; UID 10001"


def compose(tmp: Path) -> str:
    _run("compose_pins or compose_runs_the_migration")
    return "images pinned; migrate one-shot before api/worker/cdc/debezium-init; .env optional"


def helm_and_kustomize(tmp: Path) -> str:
    _run("example_values_file or values_path or required_object or runbook or kustomize_tree")
    examples = sorted(p.stem for p in (ROOT / "deploy/helm/oran-adapt/examples").glob("*.yaml"))
    return f"schema-valid: {', '.join(examples)}, ci/kind-values; alerts -> runbooks"


def no_environment_literals(tmp: Path) -> str:
    _run("environment_specific_literal or setting_the_manifests_pass")
    return "templates, values.yaml, kustomize base: no IP, host, namespace or registry literal"


def probes_and_shutdown(tmp: Path) -> str:
    _run("liveness_file or health_window or worker_loop or cli_reports")
    return "liveness file beat/check, worker health CLI, WORKER_HEALTH_MAX_AGE_S validator"


def expand_contract(tmp: Path) -> str:
    _run("only_expands or schema_status or db_wait")
    return "upgrade() only expands; db status behind/at_head/ahead; db wait times out"


def ci_wiring(tmp: Path) -> str:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    packaging = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["packaging"]["steps"])
    for needle in ("helm lint", "helm template", "examples/values-", "kubectl kustomize",
                   "kubeconform"):
        assert needle in packaging, f"packaging job lacks {needle!r}"
    images = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["build-images"]["steps"])
    assert "--sbom=true" in images and "for target in api worker migrator" in images
    smoke = "\n".join(str(s.get("run", "")) for s in ci["jobs"]["phase-0-baseline"]["steps"])
    assert "scripts/ci/compose_smoke.sh" in smoke
    e2e = yaml.safe_load((ROOT / ".github/workflows/k8s-e2e.yml").read_text(encoding="utf-8"))
    triggers = e2e[True] if True in e2e else e2e["on"]  # YAML 1.1 reads `on` as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    runs = "\n".join(str(s.get("run", "")) for s in e2e["jobs"]["kind"]["steps"])
    assert "scripts/ci/kind_e2e.sh" in runs
    script = (ROOT / "scripts/ci/kind_e2e.sh").read_text(encoding="utf-8")
    for step in ("helm install", "helm upgrade", "helm rollback"):
        assert step in script, step
    return "packaging, build-images (SBOM), compose smoke, weekly kind e2e: wired (run in CI)"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("images: targets, pinned bases, non-root, health checks", images),
    ("compose: pinned, migrate once, fresh clone", compose),
    ("Helm chart and kustomize tree", helm_and_kustomize),
    ("no environment-specific literal in any manifest", no_environment_literals),
    ("probes and graceful shutdown", probes_and_shutdown),
    ("expand/contract migrations", expand_contract),
    ("CI runs what the laptop cannot", ci_wiring),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase11-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 11 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
