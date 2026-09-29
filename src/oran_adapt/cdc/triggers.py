"""Changelog triggers for any source table, so the polling CDC source can follow a table other
than the kpi_sample one migration 0005 installs.

``trigger_sql`` returns the statements that make every insert, update and delete on ``table``
append a row to cdc_changelog, in the same shape as migration 0005: the row images as JSON
objects of the table's columns, the primary key from the key column (CDC_KEY_COLUMN). The
changelog rows are then read with a CdcRowMapping naming the same columns. The statements are
printed for an operator to review and apply (``oran-adapt cdc trigger-sql``); nothing here
changes a database by itself.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from oran_adapt.core.errors import ConfigurationError

DIALECTS = ("sqlite", "postgresql")

_CHANGELOG_COLUMNS = "table_name, operation, pk, old_row, new_row, tx_id, changed_at"
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _identifier(name: str, what: str) -> str:
    """Only plain identifiers: they are interpolated into DDL, so anything else is refused
    rather than quoted."""
    if not _IDENTIFIER.match(name or ""):
        raise ConfigurationError(f"{what} '{name}' is not a plain SQL identifier", key=what)
    return name


def _sqlite_row(ref: str, columns: Sequence[str], json_columns: Sequence[str]) -> str:
    pairs = ", ".join(
        f"'{c}', " + (f"json({ref}.{c})" if c in json_columns else f"{ref}.{c}")
        for c in columns
    )
    return f"json_object({pairs})"


def _sqlite(table: str, key: str, columns: Sequence[str],
            json_columns: Sequence[str]) -> list[str]:
    new, old = _sqlite_row("NEW", columns, json_columns), _sqlite_row("OLD", columns, json_columns)
    head = f"INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES ('{table}'"
    return [
        (f"CREATE TRIGGER {table}_cdc_insert AFTER INSERT ON {table}\n"
         f"BEGIN {head}, 'INSERT', CAST(NEW.{key} AS TEXT), NULL, {new}, NULL, "
         f"{_SQLITE_NOW}); END"),
        (f"CREATE TRIGGER {table}_cdc_update AFTER UPDATE ON {table}\n"
         f"BEGIN {head}, 'UPDATE', CAST(NEW.{key} AS TEXT), {old}, {new}, NULL, "
         f"{_SQLITE_NOW}); END"),
        (f"CREATE TRIGGER {table}_cdc_delete AFTER DELETE ON {table}\n"
         f"BEGIN {head}, 'DELETE', CAST(OLD.{key} AS TEXT), {old}, NULL, NULL, "
         f"{_SQLITE_NOW}); END"),
    ]


def _postgresql(table: str, key: str) -> list[str]:
    return [
        f"""CREATE OR REPLACE FUNCTION {table}_cdc() RETURNS trigger AS $$
BEGIN
  INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES (
    TG_TABLE_NAME, TG_OP,
    CASE WHEN TG_OP = 'DELETE' THEN OLD.{key} ELSE NEW.{key} END::text,
    CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE row_to_json(OLD)::text END,
    CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE row_to_json(NEW)::text END,
    txid_current()::text,
    to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
  RETURN NULL;
END;
$$ LANGUAGE plpgsql""",
        (f"CREATE TRIGGER {table}_cdc AFTER INSERT OR UPDATE OR DELETE ON {table}\n"
         f"FOR EACH ROW EXECUTE FUNCTION {table}_cdc()"),
    ]


def trigger_sql(
    table: str,
    *,
    dialect: str,
    key_column: str = "id",
    columns: Sequence[str] = (),
    json_columns: Sequence[str] = (),
) -> list[str]:
    """The DDL statements for ``table``. SQLite has no row-to-JSON function, so it needs the
    column list (``columns``, which must include ``key_column``; ``json_columns`` hold JSON text
    and are embedded as JSON rather than as strings). PostgreSQL serializes the whole row."""
    table = _identifier(table, "CDC_POLLING_TABLE")
    key = _identifier(key_column, "CDC_KEY_COLUMN")
    if dialect == "postgresql":
        return _postgresql(table, key)
    if dialect != "sqlite":
        raise ConfigurationError(
            f"unknown dialect '{dialect}' (expected one of {', '.join(DIALECTS)})",
            key="dialect",
        )
    names = [_identifier(c, "columns") for c in columns]
    if not names:
        raise ConfigurationError("SQLite triggers need the table's column list", key="columns")
    if key not in names:
        raise ConfigurationError(
            f"the key column '{key}' is not among the columns", key="CDC_KEY_COLUMN"
        )
    unknown = sorted(set(json_columns) - set(names))
    if unknown:
        raise ConfigurationError(f"JSON columns not among the columns: {unknown}",
                                 key="json_columns")
    return _sqlite(table, key, names, list(json_columns))
