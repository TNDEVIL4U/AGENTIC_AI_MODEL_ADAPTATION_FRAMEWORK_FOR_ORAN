"""data version content hash and extra metadata

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23 14:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('data_version', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('extra', sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f('ix_data_version_content_hash'), ['content_hash'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('data_version', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_data_version_content_hash'))
        batch_op.drop_column('extra')
        batch_op.drop_column('content_hash')
