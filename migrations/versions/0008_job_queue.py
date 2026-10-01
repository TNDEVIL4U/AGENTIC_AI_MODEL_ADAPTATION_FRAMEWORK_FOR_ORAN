"""Job queue: adaptation_job gains the columns workers claim, lease, fence, cancel and reap by;
job_slot holds one row per running job of a tenant with a concurrency limit

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

_COLUMNS = (
    'tenant', 'worker_class', 'priority', 'attempt', 'available_at', 'deadline_at',
    'lease_owner', 'lease_token', 'lease_expires_at', 'cancel_requested_at',
    'cancel_requested_by', 'lost_count', 'quarantined', 'published_at',
)


def upgrade() -> None:
    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant', sa.String(length=100), nullable=False,
                                      server_default='default'))
        batch_op.add_column(sa.Column('worker_class', sa.String(length=50), nullable=False,
                                      server_default='default'))
        batch_op.add_column(sa.Column('priority', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('attempt', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('available_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('lease_owner', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('lease_token', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column('cancel_requested_at', sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column('cancel_requested_by', sa.String(length=200),
                                      nullable=True))
        batch_op.add_column(sa.Column('lost_count', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('quarantined', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('published_at', sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.create_index('ix_adaptation_job_claim',
                              ['status', 'worker_class', 'available_at'])
        batch_op.create_index('ix_adaptation_job_tenant', ['tenant'])
        batch_op.create_index('ix_adaptation_job_lease_expires_at', ['lease_expires_at'])

    op.create_table(
        'job_slot',
        sa.Column('tenant', sa.String(length=100), nullable=False),
        sa.Column('slot', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'slot'),
    )
    with op.batch_alter_table('job_slot', schema=None) as batch_op:
        batch_op.create_index('ix_job_slot_job_id', ['job_id'])


def downgrade() -> None:
    # Releases before 0008 know neither QUEUED nor CANCELLED and could not load such a row.
    # A queued job has no worker left to run it once the queue is gone: it ends FAILED, and a
    # cancelled one keeps its end as FAILED too. Resend the event after the downgrade to rerun.
    op.execute(
        "UPDATE adaptation_job SET status = 'FAILED' WHERE status IN ('QUEUED', 'CANCELLED')"
    )
    with op.batch_alter_table('job_slot', schema=None) as batch_op:
        batch_op.drop_index('ix_job_slot_job_id')
    op.drop_table('job_slot')
    with op.batch_alter_table('adaptation_job', schema=None) as batch_op:
        batch_op.drop_index('ix_adaptation_job_lease_expires_at')
        batch_op.drop_index('ix_adaptation_job_tenant')
        batch_op.drop_index('ix_adaptation_job_claim')
        for name in reversed(_COLUMNS):
            batch_op.drop_column(name)
