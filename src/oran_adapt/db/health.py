"""Database connectivity check."""

from __future__ import annotations

from sqlalchemy import Engine, text

from oran_adapt.core.errors import DatabaseUnavailableError


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise DatabaseUnavailableError("Database is not reachable", cause=str(exc)) from exc
