"""audit fields (actor, decision, reason), correlation ids, append-only audit_log

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

_SQLITE_TRIGGERS = (
    """CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
)
_POSTGRES_TRIGGER = (
    """CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;
    $$ LANGUAGE plpgsql""",
    """CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()""",
)


def upgrade() -> None:
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('actor', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('decision', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('correlation_id', sa.String(length=128), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_audit_log_correlation_id'), ['correlation_id'], unique=False
        )
    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('correlation_id', sa.String(length=128), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_adaptation_job_correlation_id'), ['correlation_id'], unique=False
        )

    # Created after the batch operations: SQLite batch mode rebuilds the table, dropping triggers.
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        for ddl in _SQLITE_TRIGGERS:
            op.execute(ddl)
    elif dialect == 'postgresql':
        for ddl in _POSTGRES_TRIGGER:
            op.execute(ddl)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_update')
        op.execute('DROP TRIGGER IF EXISTS audit_log_no_delete')
    elif dialect == 'postgresql':
        op.execute('DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log')
        op.execute('DROP FUNCTION IF EXISTS audit_log_append_only()')

    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_adaptation_job_correlation_id'))
        batch_op.drop_column('correlation_id')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_correlation_id'))
        batch_op.drop_column('correlation_id')
        batch_op.drop_column('reason')
        batch_op.drop_column('decision')
        batch_op.drop_column('actor')
