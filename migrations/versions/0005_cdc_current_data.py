"""CDC (kpi_sample source table, trigger changelog, events, offsets), dataset-version
provenance fields, record keys, persisted CurrentData

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

_CHANGELOG_COLUMNS = "table_name, operation, pk, old_row, new_row, tx_id, changed_at"
_SQLITE_ROW = (
    "json_object('id', {r}.id, 'dataset_id', {r}.dataset_id, "
    "'observed_at', {r}.observed_at, 'payload', json({r}.payload))"
)
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_SQLITE_TRIGGERS = (
    f"""CREATE TRIGGER kpi_sample_cdc_insert AFTER INSERT ON kpi_sample
    BEGIN INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES ('kpi_sample', 'INSERT',
    CAST(NEW.id AS TEXT), NULL, {_SQLITE_ROW.format(r='NEW')}, NULL, {_SQLITE_NOW}); END""",
    f"""CREATE TRIGGER kpi_sample_cdc_update AFTER UPDATE ON kpi_sample
    BEGIN INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES ('kpi_sample', 'UPDATE',
    CAST(NEW.id AS TEXT), {_SQLITE_ROW.format(r='OLD')}, {_SQLITE_ROW.format(r='NEW')}, NULL,
    {_SQLITE_NOW}); END""",
    f"""CREATE TRIGGER kpi_sample_cdc_delete AFTER DELETE ON kpi_sample
    BEGIN INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES ('kpi_sample', 'DELETE',
    CAST(OLD.id AS TEXT), {_SQLITE_ROW.format(r='OLD')}, NULL, NULL, {_SQLITE_NOW}); END""",
)
# On PostgreSQL, Debezium reads kpi_sample from the WAL; these triggers only feed the polling
# fallback, and give it the real transaction id.
_POSTGRES_TRIGGER = (
    f"""CREATE OR REPLACE FUNCTION kpi_sample_cdc() RETURNS trigger AS $$
    BEGIN
      INSERT INTO cdc_changelog ({_CHANGELOG_COLUMNS}) VALUES (
        TG_TABLE_NAME, TG_OP,
        CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END::text,
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE row_to_json(OLD)::text END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE row_to_json(NEW)::text END,
        txid_current()::text,
        to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql""",
    """CREATE TRIGGER kpi_sample_cdc AFTER INSERT OR UPDATE OR DELETE ON kpi_sample
    FOR EACH ROW EXECUTE FUNCTION kpi_sample_cdc()""",
)


def upgrade() -> None:
    with op.batch_alter_table('data_version', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('schema_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('storage_uri', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column(
            'status', sa.String(length=20), nullable=False, server_default='AVAILABLE'
        ))
        batch_op.add_column(sa.Column('cdc_range', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('source_tx', sa.JSON(), nullable=True))
    with op.batch_alter_table('data_record', schema=None) as batch_op:
        batch_op.add_column(sa.Column('record_key', sa.String(length=200), nullable=True))
        batch_op.create_index(batch_op.f('ix_data_record_record_key'), ['record_key'])
    with op.batch_alter_table('model_version_evaluation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('current_data_id', sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_model_version_evaluation_current_data_id'), ['current_data_id']
        )

    op.create_table(
        'current_data',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('current_data_id', sa.String(length=64), nullable=False),
        sa.Column('data_version_id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('source_versions', sa.JSON(), nullable=False),
        sa.Column('data_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('data_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('schema', sa.JSON(), nullable=False),
        sa.Column('schema_hash', sa.String(length=64), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('quality', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['data_version_id'], ['data_version.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('current_data', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_current_data_current_data_id'), ['current_data_id'], unique=True
        )
        batch_op.create_index(batch_op.f('ix_current_data_data_version_id'), ['data_version_id'])
        batch_op.create_index(batch_op.f('ix_current_data_model_id'), ['model_id'])
        batch_op.create_index(batch_op.f('ix_current_data_job_id'), ['job_id'])
        batch_op.create_index(batch_op.f('ix_current_data_content_hash'), ['content_hash'])

    op.create_table(
        'kpi_sample',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('dataset_id', sa.String(length=200), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('kpi_sample', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kpi_sample_dataset_id'), ['dataset_id'])

    op.create_table(
        'cdc_changelog',
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('table_name', sa.String(length=100), nullable=False),
        sa.Column('operation', sa.String(length=10), nullable=False),
        sa.Column('pk', sa.String(length=200), nullable=False),
        sa.Column('old_row', sa.Text(), nullable=True),
        sa.Column('new_row', sa.Text(), nullable=True),
        sa.Column('tx_id', sa.String(length=64), nullable=True),
        sa.Column('changed_at', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('seq'),
    )

    op.create_table(
        'cdc_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('source_table', sa.String(length=100), nullable=False),
        sa.Column('operation', sa.String(length=10), nullable=False),
        sa.Column('primary_key', sa.String(length=200), nullable=False),
        sa.Column('dataset_id', sa.String(length=200), nullable=True),
        sa.Column('old_value', sa.JSON(), nullable=True),
        sa.Column('new_value', sa.JSON(), nullable=True),
        sa.Column('event_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('transaction_id', sa.String(length=64), nullable=True),
        sa.Column('source_offset', sa.String(length=200), nullable=False),
        sa.Column('schema_version', sa.String(length=50), nullable=False),
        sa.Column('data_version_id', sa.Integer(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['data_version_id'], ['data_version.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('cdc_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cdc_event_event_id'), ['event_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_cdc_event_primary_key'), ['primary_key'])
        batch_op.create_index(batch_op.f('ix_cdc_event_dataset_id'), ['dataset_id'])
        batch_op.create_index(batch_op.f('ix_cdc_event_data_version_id'), ['data_version_id'])

    op.create_table(
        'cdc_offset',
        sa.Column('consumer', sa.String(length=200), nullable=False),
        sa.Column('position', sa.String(length=200), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('consumer'),
    )

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
        for name in ('kpi_sample_cdc_insert', 'kpi_sample_cdc_update', 'kpi_sample_cdc_delete'):
            op.execute(f'DROP TRIGGER IF EXISTS {name}')
    elif dialect == 'postgresql':
        op.execute('DROP TRIGGER IF EXISTS kpi_sample_cdc ON kpi_sample')
        op.execute('DROP FUNCTION IF EXISTS kpi_sample_cdc()')

    op.drop_table('cdc_offset')
    with op.batch_alter_table('cdc_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cdc_event_data_version_id'))
        batch_op.drop_index(batch_op.f('ix_cdc_event_dataset_id'))
        batch_op.drop_index(batch_op.f('ix_cdc_event_primary_key'))
        batch_op.drop_index(batch_op.f('ix_cdc_event_event_id'))
    op.drop_table('cdc_event')
    op.drop_table('cdc_changelog')
    with op.batch_alter_table('kpi_sample', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kpi_sample_dataset_id'))
    op.drop_table('kpi_sample')
    with op.batch_alter_table('current_data', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_current_data_content_hash'))
        batch_op.drop_index(batch_op.f('ix_current_data_job_id'))
        batch_op.drop_index(batch_op.f('ix_current_data_model_id'))
        batch_op.drop_index(batch_op.f('ix_current_data_data_version_id'))
        batch_op.drop_index(batch_op.f('ix_current_data_current_data_id'))
    op.drop_table('current_data')

    with op.batch_alter_table('model_version_evaluation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_model_version_evaluation_current_data_id'))
        batch_op.drop_column('current_data_id')
    with op.batch_alter_table('data_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_data_record_record_key'))
        batch_op.drop_column('record_key')
    with op.batch_alter_table('data_version', schema=None) as batch_op:
        batch_op.drop_column('source_tx')
        batch_op.drop_column('cdc_range')
        batch_op.drop_column('status')
        batch_op.drop_column('storage_uri')
        batch_op.drop_column('schema_hash')
        batch_op.drop_column('source')
