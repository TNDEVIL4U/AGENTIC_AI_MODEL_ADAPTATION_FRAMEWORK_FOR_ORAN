"""Notification outbox: notification_event (one row per event) and notification_delivery (one
row per event and sink, with its retry state)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'notification_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=True),
        sa.Column('envelope', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notification_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_event_event_id'), ['event_id'],
                              unique=True)
        batch_op.create_index(batch_op.f('ix_notification_event_event_type'), ['event_type'])
        batch_op.create_index(batch_op.f('ix_notification_event_subject'), ['subject'])
        batch_op.create_index(batch_op.f('ix_notification_event_model_id'), ['model_id'])

    op.create_table(
        'notification_delivery',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('sink', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('leased_by', sa.String(length=100), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_status_code', sa.Integer(), nullable=True),
        sa.Column('redrive_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dead_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['event_id'], ['notification_event.event_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'sink'),
    )
    with op.batch_alter_table('notification_delivery', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_delivery_event_id'), ['event_id'])
        batch_op.create_index(batch_op.f('ix_notification_delivery_sink'), ['sink'])
        batch_op.create_index(batch_op.f('ix_notification_delivery_status'), ['status'])
        batch_op.create_index(batch_op.f('ix_notification_delivery_next_attempt_at'),
                              ['next_attempt_at'])


def downgrade() -> None:
    with op.batch_alter_table('notification_delivery', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_delivery_next_attempt_at'))
        batch_op.drop_index(batch_op.f('ix_notification_delivery_status'))
        batch_op.drop_index(batch_op.f('ix_notification_delivery_sink'))
        batch_op.drop_index(batch_op.f('ix_notification_delivery_event_id'))
    op.drop_table('notification_delivery')
    with op.batch_alter_table('notification_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_event_model_id'))
        batch_op.drop_index(batch_op.f('ix_notification_event_subject'))
        batch_op.drop_index(batch_op.f('ix_notification_event_event_type'))
        batch_op.drop_index(batch_op.f('ix_notification_event_event_id'))
    op.drop_table('notification_event')
