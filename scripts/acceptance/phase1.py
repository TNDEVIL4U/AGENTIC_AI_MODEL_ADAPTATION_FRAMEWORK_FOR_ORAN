"""Hardening Phase 1 acceptance: the gate criteria, checked end to end.

Run by scripts/verify.sh 1 after the lint, import-boundary and no-gaps steps (criteria 1 and 2).
This script checks the rest against the installed package, the real CLI in a child process and a
real app instance:

3. A selected adapter whose required key is unset stops startup, naming the key; so does the
   production profile with a defaulted storage location.
4. ``oran-adapt config lint`` passes every shipped example config.
5. ``GET /api/v1/capabilities`` lists every adapter registered under every port.

Each child process runs in an empty temporary directory (no ``.env``) with the configuration keys
it depends on removed from its environment. Exit status 0 means every check passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "config" / "examples").glob("*.toml"))
# Keys a developer shell may set that would change what these checks see.
SCRUBBED = ("ORAN_CONFIG_FILE", "ENVIRONMENT", "CDC_MODE", "KAFKA_BOOTSTRAP_SERVERS",
            "DATABASE_URL", "MLFLOW_TRACKING_URI", "ARTIFACT_WORKDIR")


def _cli(args: list[str], cwd: Path, **env: str) -> tuple[int, dict]:
    child_env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    child_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "oran_adapt.cli", *args],
        cwd=cwd, env=child_env, check=False, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"oran-adapt {' '.join(args)} printed no JSON: {proc.stderr[-800:]}") from exc
    return proc.returncode, body


def missing_adapter_key_fails_startup(tmp: Path) -> str:
    code, body = _cli(["config", "effective"], tmp, CDC_MODE="kafka")
    assert code == 1, f"exit {code}, expected 1: {body}"
    assert body["code"] == "CONFIGURATION_ERROR", body
    assert body["context"]["key"] == "KAFKA_BOOTSTRAP_SERVERS", body
    assert "KAFKA_BOOTSTRAP_SERVERS" in body["message"], body
    return body["message"]


def production_refuses_defaulted_storage(tmp: Path) -> str:
    code, body = _cli(["config", "effective"], tmp, ENVIRONMENT="production")
    assert code == 1 and body["code"] == "CONFIGURATION_ERROR", body
    assert body["context"]["missing"] == ["DATABASE_URL", "MLFLOW_TRACKING_URI", "ARTIFACT_WORKDIR"], body
    return body["message"]


def examples_pass_lint(tmp: Path) -> str:
    assert EXAMPLES, "config/examples/*.toml is empty"
    code, body = _cli(["config", "lint", *map(str, EXAMPLES)], tmp)
    assert code == 0, body
    return f"{len(EXAMPLES)} example(s) clean: {', '.join(p.name for p in EXAMPLES)}"


def capabilities_lists_every_adapter(tmp: Path) -> str:
    from fastapi.testclient import TestClient

    from oran_adapt import plugins
    from oran_adapt.api.app import create_app
    from oran_adapt.core.config import Settings
    from oran_adapt.db.migrate import upgrade_to_head

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp / "work"),
        log_json=False,
        auth_enabled=False,
    )
    upgrade_to_head(settings.database_url)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/capabilities")
    assert response.status_code == 200, response.text
    served = {port: {a["adapter"] for a in info["adapters"]} for port, info in response.json()["ports"].items()}
    installed = {port: set(plugins.adapters(port)) for port in plugins.PORTS}
    assert served == installed, f"served {served} != installed {installed}"
    return "; ".join(f"{port}: {', '.join(sorted(names))}" for port, names in sorted(served.items()))


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("missing required adapter key fails startup, naming it", missing_adapter_key_fails_startup),
    ("production refuses defaulted storage locations", production_refuses_defaulted_storage),
    ("config lint passes every example config", examples_pass_lint),
    ("GET /api/v1/capabilities lists every registered adapter", capabilities_lists_every_adapter),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase1-", ignore_cleanup_errors=True) as tmp:
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}")
            else:
                print(f"PASS  {name}\n      {detail}")
    print(f"\nphase 1 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
