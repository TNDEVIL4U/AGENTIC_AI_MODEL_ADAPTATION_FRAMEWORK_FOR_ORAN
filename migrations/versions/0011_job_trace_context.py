"""Job trace context: adaptation_job.trace_context holds the W3C traceparent of the job's intake
span, so the worker that claims the job continues the drift event's trace

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trace_context', sa.String(length=128), nullable=True))


def downgrade() -> None:
    # Releases before 0011 start a new trace per attempt; nothing else reads the column.
    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.drop_column('trace_context')
