"""Validation gate decisions and progressive delivery: gate_decision holds every verdict of
the gate with its policy hash; rollout and rollout_observation hold a candidate's way to traffic

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29 21:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'gate_decision',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('current_version', sa.String(length=50), nullable=True),
        sa.Column('candidate_version', sa.String(length=50), nullable=True),
        sa.Column('verdict', sa.String(length=10), nullable=False),
        sa.Column('metric', sa.String(length=50), nullable=False),
        sa.Column('policy_version', sa.String(length=50), nullable=False),
        sa.Column('policy_hash', sa.String(length=64), nullable=False),
        sa.Column('decision', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('gate_decision', schema=None) as batch_op:
        batch_op.create_index('ix_gate_decision_model_id', ['model_id'])
        batch_op.create_index('ix_gate_decision_job_id', ['job_id'])
        batch_op.create_index('ix_gate_decision_verdict', ['verdict'])

    op.create_table(
        'rollout',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rollout_id', sa.String(length=64), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('strategy', sa.String(length=20), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('stable_version', sa.String(length=50), nullable=True),
        sa.Column('candidate_version', sa.String(length=50), nullable=False),
        sa.Column('step', sa.Integer(), nullable=False),
        sa.Column('percent', sa.Integer(), nullable=False),
        sa.Column('policy', sa.JSON(), nullable=False),
        sa.Column('policy_hash', sa.String(length=64), nullable=False),
        sa.Column('gate_decision_id', sa.Integer(), nullable=True),
        sa.Column('step_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.String(length=200), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('history', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('rollout', schema=None) as batch_op:
        batch_op.create_index('ix_rollout_rollout_id', ['rollout_id'], unique=True)
        batch_op.create_index('ix_rollout_model_id', ['model_id'])
        batch_op.create_index('ix_rollout_job_id', ['job_id'])
        batch_op.create_index('ix_rollout_state', ['state'])

    op.create_table(
        'rollout_observation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rollout_id', sa.String(length=64), nullable=False),
        sa.Column('arm', sa.String(length=20), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('rollout_observation', schema=None) as batch_op:
        batch_op.create_index('ix_rollout_observation_rollout_id', ['rollout_id'])
        batch_op.create_index('ix_rollout_observation_observed_at', ['observed_at'])


def downgrade() -> None:
    # Releases before 0009 have no rollouts: a candidate still in one simply never goes live
    # (its registry version stays registered, the live alias untouched). Drop in reverse.
    with op.batch_alter_table('rollout_observation', schema=None) as batch_op:
        batch_op.drop_index('ix_rollout_observation_observed_at')
        batch_op.drop_index('ix_rollout_observation_rollout_id')
    op.drop_table('rollout_observation')
    with op.batch_alter_table('rollout', schema=None) as batch_op:
        batch_op.drop_index('ix_rollout_state')
        batch_op.drop_index('ix_rollout_job_id')
        batch_op.drop_index('ix_rollout_model_id')
        batch_op.drop_index('ix_rollout_rollout_id')
    op.drop_table('rollout')
    with op.batch_alter_table('gate_decision', schema=None) as batch_op:
        batch_op.drop_index('ix_gate_decision_verdict')
        batch_op.drop_index('ix_gate_decision_job_id')
        batch_op.drop_index('ix_gate_decision_model_id')
    op.drop_table('gate_decision')
