"""Regenerate docs/api/openapi.json: the OpenAPI document of the API the code serves.

The app is built with throwaway local settings (a temporary SQLite database and MLflow store;
nothing is contacted) and its schema written with sorted keys, so the file changes only when a
route, a model or a description changes. The REST contract is additive: compare a regenerated
file with the committed one to see what a change adds.

Usage:
    python scripts/openapi.py           # rewrite the file
    python scripts/openapi.py --check   # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api" / "openapi.json"


def render() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from oran_adapt.api.app import create_app
    from oran_adapt.core.config import Settings

    # The routes, not the adapters, make the schema: the filesystem registry keeps the render
    # fast (no MLflow store to create). ignore_cleanup_errors: Windows can hold SQLite files.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{Path(tmp, 'app.db').as_posix()}",
            registry_backend="filesystem",
            registry_fs_root=str(Path(tmp, "registry")),
            artifact_store_root=str(Path(tmp, "artifacts")),
            artifact_workdir=str(Path(tmp, "work")),
            notification_dispatch_enabled=False,
        )
        schema = create_app(settings).openapi()
    return json.dumps(schema, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args(argv)
    text = render()
    rel = OUT.relative_to(ROOT).as_posix()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{rel} is stale: run python scripts/openapi.py")
            return 1
        print(f"{rel} is up to date ({len(json.loads(text)['paths'])} paths)")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {rel} ({len(json.loads(text)['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
