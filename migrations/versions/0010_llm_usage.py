"""LLM usage ledger: llm_usage holds every LLM call's tokens and cost, the record the token and
cost budgets are checked against

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'llm_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=100), nullable=False),
        sa.Column('prompt_id', sa.String(length=100), nullable=True),
        sa.Column('prompt_version', sa.String(length=50), nullable=True),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('estimated', sa.Boolean(), nullable=False),
        sa.Column('cost', sa.Float(), nullable=False),
        sa.Column('outcome', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.create_index('ix_llm_usage_provider', ['provider'])
        batch_op.create_index('ix_llm_usage_job_id', ['job_id'])
        batch_op.create_index('ix_llm_usage_created_at', ['created_at'])


def downgrade() -> None:
    # Releases before 0010 keep no usage ledger: budgets start from zero after an upgrade.
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.drop_index('ix_llm_usage_created_at')
        batch_op.drop_index('ix_llm_usage_job_id')
        batch_op.drop_index('ix_llm_usage_provider')
    op.drop_table('llm_usage')
