"""Programmatic Alembic helpers (used by tests, the CLI, the migration job and the demo
scripts).

Migrations follow expand/contract (docs/operations/migrations.md): an ``upgrade`` only adds, so
the previous release keeps working on the new schema while a rollout is in progress. That is
why ``schema_status`` counts a revision this release does not know as ``ahead``: a newer release
migrated the database, and this one can still run against it.
"""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from oran_adapt.core.errors import SchemaNotReadyError

ROOT = Path(__file__).resolve().parents[3]


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_to_head(url: str) -> None:
    command.upgrade(_config(url), "head")


def downgrade_to_base(url: str) -> None:
    command.downgrade(_config(url), "base")


def schema_status(url: str) -> dict[str, object]:
    """Where the database's schema stands against this release's migrations: ``at_head``,
    ``behind`` (including an empty database) or ``ahead`` (migrated by a newer release)."""
    script = ScriptDirectory.from_config(_config(url))
    heads = sorted(script.get_heads())
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            current = sorted(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()
    state = "at_head" if current == heads else "behind"
    for revision in current:
        try:
            known = script.get_revision(revision) is not None
        except CommandError:
            known = False
        if not known:
            state = "ahead"
    return {"state": state, "current": current, "head": heads}


def wait_for_schema(url: str, *, timeout_s: float, interval_s: float) -> dict[str, object]:
    """Block until the schema is at or beyond this release's head (the migration job has run).
    An unreachable database counts as not ready yet. Raises SchemaNotReadyError on timeout."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {"state": "unknown"}
    while True:
        try:
            last = schema_status(url)
        except SQLAlchemyError as exc:
            last = {"state": "unreachable", "cause": type(exc).__name__}
        if last["state"] in {"at_head", "ahead"}:
            return last
        if time.monotonic() >= deadline:
            raise SchemaNotReadyError(
                "the database schema did not reach this release's migrations in time",
                timeout_s=timeout_s, **last,
            )
        time.sleep(interval_s)
