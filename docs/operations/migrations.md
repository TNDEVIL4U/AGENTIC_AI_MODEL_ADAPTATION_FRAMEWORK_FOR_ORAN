# Schema migrations

Alembic migrations under `src/oran_adapt/db/migrations/`. The API never changes the schema: a
separate one-shot step does (`oran-adapt db upgrade`, the `migrator` image), and every other
process waits until the schema matches the code it runs.

## Expand / contract

A release's `upgrade()` only **expands** the schema: new tables, new nullable or defaulted
columns, new indexes. It never drops, renames or narrows. So while the migration runs, and while
a rolling update has old and new pods side by side, the old code still finds everything it
reads.

A contraction (dropping a column the code no longer reads) ships in a **later** release, after
every pod runs code that no longer needs it. That release's migration is the only place a drop
may appear, and its review states which earlier release stopped reading the column.

`test_every_migration_upgrade_only_expands_the_schema` parses every migration's `upgrade()` and
fails on `drop_*`, `rename_*`, `alter_column`, a NOT NULL `add_column` without a
`server_default`, and raw `DROP` / `RENAME` / `TRUNCATE` / `ALTER COLUMN` SQL. The test has no
allow-list: a deliberate contraction changes the test in the same review as the migration.

Rollback follows from this: `helm rollback` (or re-applying the previous overlay) brings back
the previous code, which works on the expanded schema. No deployment path downgrades the
database; `oran_adapt.db.migrate.downgrade_to_base` exists for tests only.

## Commands

| Command | What it does | Exit status |
|---------|--------------|-------------|
| `oran-adapt db upgrade` | Applies every pending migration | 0; non-zero on failure |
| `oran-adapt db status` | Prints `{"state", "current", "head"}`; `state` is `at_head`, `behind` (pending migrations, or an empty database) or `ahead` (the database carries a revision this code does not know: a newer release migrated it) | 0; non-zero if the database is unreachable |
| `oran-adapt db wait [--timeout-s N]` | Polls until the state is `at_head` or `ahead` | 0; 1 with `SCHEMA_NOT_READY` and the last state after the timeout |

`db wait` accepts `ahead` on purpose: during a rolling update the migration for release N+1 has
already run while pods of release N are still starting, and expand-only migrations make that
safe.

| Setting | Default | Meaning |
|---------|---------|---------|
| `MIGRATION_WAIT_TIMEOUT_S` | 600 | How long `db wait` waits (overridden by `--timeout-s`) |
| `MIGRATION_WAIT_INTERVAL_S` | 2 | How often it looks |

## Where it runs

- **Compose:** the `migrate` service runs once; `api`, `worker`, `cdc-consumer` and
  `debezium-init` start only after it completed successfully.
- **Helm:** a `pre-install,pre-upgrade` hook Job (weight 0). Pods start with a `wait-for-schema`
  init container (`oran-adapt db wait`). See [helm.md](helm.md#hooks).
- **Kustomize:** a plain Job, renamed per release by the overlay, and the same init containers.
  See [kustomize.md](kustomize.md).
