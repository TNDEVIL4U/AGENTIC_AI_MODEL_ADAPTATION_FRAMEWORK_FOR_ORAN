"""model promotion history, per-model job lock, version evaluations

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23 22:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'model_promotion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('from_version', sa.String(length=50), nullable=True),
        sa.Column('to_version', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('actor', sa.String(length=100), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('idempotency_key', sa.String(length=200), nullable=True),
        sa.Column('artifact_sha256', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['model_id'], ['model_metadata.model_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key'),
    )
    with op.batch_alter_table('model_promotion', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_promotion_model_id'), ['model_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_promotion_to_version'), ['to_version'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_promotion_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_promotion_job_id'), ['job_id'], unique=False)

    op.create_table(
        'model_lock',
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('model_id'),
    )

    op.create_table(
        'model_version_evaluation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=False),
        sa.Column('is_live', sa.Boolean(), nullable=False),
        sa.Column('compatible', sa.Boolean(), nullable=False),
        sa.Column('reusable', sa.Boolean(), nullable=False),
        sa.Column('metric_name', sa.String(length=50), nullable=True),
        sa.Column('metric_value', sa.Float(), nullable=True),
        sa.Column('reuse_score', sa.Float(), nullable=True),
        sa.Column('n_rows', sa.Integer(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('model_version_evaluation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_version_evaluation_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_model_version_evaluation_model_id'), ['model_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_model_version_evaluation_model_version'), ['model_version'], unique=False
        )

    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_model_id'), ['model_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_log_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_created_at'))
        batch_op.drop_index(batch_op.f('ix_audit_log_model_id'))
    op.drop_table('model_version_evaluation')
    op.drop_table('model_lock')
    op.drop_table('model_promotion')
