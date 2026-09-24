"""kpi_sample REPLICA IDENTITY FULL (PostgreSQL), so Debezium DELETE events carry the whole row

With PostgreSQL's default replica identity, a DELETE's "before" image holds only the primary
key. A CDC event is filed under its row's dataset_id, so a delete without it could never be
folded into its dataset's next version. FULL logs the complete old row. No-op elsewhere (the
SQLite triggers already record the full old row).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26 10:00:00
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE kpi_sample REPLICA IDENTITY FULL")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE kpi_sample REPLICA IDENTITY DEFAULT")
