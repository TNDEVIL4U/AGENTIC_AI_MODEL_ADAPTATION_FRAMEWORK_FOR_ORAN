"""PostgreSQL data model.

PostgreSQL owns datasets, data versions, lineage, model<->data links, jobs and audit.
It deliberately does NOT own model versions or artifacts: MLflow is the only authority for
those. The `model_version` columns below are *references* to MLflow versions, not a registry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oran_adapt.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelMetadata(Base):
    __tablename__ = "model_metadata"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    mlflow_model_name: Mapped[str] = mapped_column(String(200))
    model_type: Mapped[str | None] = mapped_column(String(100))
    framework: Mapped[str | None] = mapped_column(String(50))
    task_type: Mapped[str | None] = mapped_column(String(50))
    target_column: Mapped[str | None] = mapped_column(String(200))
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class DatasetMetadata(Base):
    __tablename__ = "dataset_metadata"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
    versions: Mapped[list[DataVersion]] = relationship(back_populates="dataset")


class DataVersion(Base):
    __tablename__ = "data_version"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset_metadata.id"), index=True)
    version: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))  # DataKind
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("data_version.id"))
    data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # SHA-256 of the version's canonical content (see datastore.versioning.content_hash) - what
    # makes a version name immutable: re-ingesting it with different rows is a conflict.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Column schema, source and producer (e.g. which adaptation job snapshotted it).
    extra: Mapped[dict | None] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = _ts()
    # Where the version came from (upload, CDC, adaptation snapshot, CurrentData build).
    source: Mapped[str | None] = mapped_column(Text)
    # SHA-256 of the column -> dtype map: equal schema hashes mean interchangeable versions.
    schema_hash: Mapped[str | None] = mapped_column(String(64))
    # Where the rows live. Today always the data_record table of this database.
    storage_uri: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    # For CDC-derived versions: the changelog/Kafka offsets and source transactions it covers.
    cdc_range: Mapped[dict | None] = mapped_column(JSON)
    source_tx: Mapped[list | None] = mapped_column(JSON)
    dataset: Mapped[DatasetMetadata] = relationship(back_populates="versions")
    records: Mapped[list[DataRecord]] = relationship(back_populates="data_version")


class DataRecord(Base):
    """One observation row of a data version (feature/target payload with its timestamp)."""

    __tablename__ = "data_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    data_version_id: Mapped[int] = mapped_column(ForeignKey("data_version.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # Primary key of the source row (CDC-derived rows), used to resolve conflicting versions of
    # the same row when CurrentData is built. None for uploaded files.
    record_key: Mapped[str | None] = mapped_column(String(200), index=True)
    data_version: Mapped[DataVersion] = relationship(back_populates="records")


class ModelDataAssociation(Base):
    __tablename__ = "model_data_association"
    __table_args__ = (UniqueConstraint("model_id", "model_version", "data_version_id", "role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_metadata.model_id"), index=True)
    model_version: Mapped[str] = mapped_column(String(50))  # reference to MLflow version
    data_version_id: Mapped[int] = mapped_column(ForeignKey("data_version.id"))
    role: Mapped[str] = mapped_column(String(30))  # AssociationRole
    created_at: Mapped[datetime] = _ts()


class PerformanceRecord(Base):
    __tablename__ = "performance_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_metadata.model_id"), index=True)
    model_version: Mapped[str] = mapped_column(String(50))
    data_version_id: Mapped[int | None] = mapped_column(ForeignKey("data_version.id"))
    metric_name: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = _ts()


class AdaptationJob(Base):
    __tablename__ = "adaptation_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    strategy: Mapped[str | None] = mapped_column(String(40))
    event: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    events: Mapped[list[AdaptationEvent]] = relationship(back_populates="job")


class AdaptationEvent(Base):
    """State-transition / step record for a job."""

    __tablename__ = "adaptation_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("adaptation_job.job_id"), index=True)
    component: Mapped[str] = mapped_column(String(50))
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str | None] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()
    job: Mapped[AdaptationJob] = relationship(back_populates="events")


class ModelPromotion(Base):
    """Every move of a model's live alias: a validated candidate going live, an older version
    reused, or a rollback. ``from_version`` is what LIVE pointed at before, so any move can be
    undone. ``idempotency_key`` makes a repeated request return the first result."""

    __tablename__ = "model_promotion"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_metadata.model_id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # PromotionKind
    from_version: Mapped[str | None] = mapped_column(String(50))
    to_version: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # APPLIED | NO_CHANGE
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


class ModelLock(Base):
    """At most one running adaptation job per model. The primary key makes acquiring it a
    single INSERT that only one caller can win, on SQLite and PostgreSQL alike."""

    __tablename__ = "model_lock"
    model_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64))
    acquired_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelVersionEvaluation(Base):
    """How one registered version scored on the current data during one job - the evidence
    behind a reuse decision."""

    __tablename__ = "model_version_evaluation"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    model_version: Mapped[str] = mapped_column(String(50), index=True)
    is_live: Mapped[bool] = mapped_column(default=False)
    compatible: Mapped[bool] = mapped_column(default=False)
    reusable: Mapped[bool] = mapped_column(default=False)
    metric_name: Mapped[str | None] = mapped_column(String(50))
    metric_value: Mapped[float | None] = mapped_column(Float)
    reuse_score: Mapped[float | None] = mapped_column(Float)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON)
    # The CurrentData the version was scored on.
    current_data_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    component: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str | None] = mapped_column(String(200), index=True)
    model_version: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="OK")
    actor: Mapped[str | None] = mapped_column(String(100))
    decision: Mapped[str | None] = mapped_column(String(60))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON)  # the event's metadata
    error: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class CurrentData(Base):
    """The cleaned data one job evaluated versions and validated its candidate on: which data
    versions it came from, what cleaning removed, and the immutable CURRENT data version that
    holds its rows. Answers "which data was this decision made on?"."""

    __tablename__ = "current_data"
    id: Mapped[int] = mapped_column(primary_key=True)
    current_data_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    data_version_id: Mapped[int] = mapped_column(ForeignKey("data_version.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_versions: Mapped[list] = mapped_column(JSON)
    data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    schema: Mapped[dict] = mapped_column(JSON)
    schema_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    quality: Mapped[dict] = mapped_column(JSON)  # rows removed by each cleaning step
    created_at: Mapped[datetime] = _ts()


class KpiSample(Base):
    """The framework-owned source table that CDC watches: one KPI observation per row, keyed by
    ``id``. Producers insert/update/delete here; Debezium (production) or the changelog
    triggers (local fallback) turn every change into a CDC event."""

    __tablename__ = "kpi_sample"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(200), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class CdcChangelog(Base):
    """Row changes captured by database triggers on the watched tables - the local-dev CDC
    fallback (``CDC_MODE=polling``). Written only by the triggers (migration 0005); ``seq`` is
    the poller's offset. Row images are JSON text as the triggers produce them."""

    __tablename__ = "cdc_changelog"
    seq: Mapped[int] = mapped_column(primary_key=True)
    table_name: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(10))  # INSERT | UPDATE | DELETE
    pk: Mapped[str] = mapped_column(String(200))
    old_row: Mapped[str | None] = mapped_column(Text)
    new_row: Mapped[str | None] = mapped_column(Text)
    tx_id: Mapped[str | None] = mapped_column(String(64))
    changed_at: Mapped[str] = mapped_column(String(40))  # ISO-8601 UTC, set by the trigger


class CdcEventRecord(Base):
    """Every CDC event the consumer accepted, once: ``event_id`` is derived from the event's
    source position, so a redelivered event hits the unique constraint and is skipped.
    ``data_version_id`` is set when the event is materialized into a data version."""

    __tablename__ = "cdc_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20))  # polling | debezium
    source_table: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(10))
    primary_key: Mapped[str] = mapped_column(String(200), index=True)
    dataset_id: Mapped[str | None] = mapped_column(String(200), index=True)
    old_value: Mapped[dict | None] = mapped_column(JSON)
    new_value: Mapped[dict | None] = mapped_column(JSON)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[str | None] = mapped_column(String(64))
    source_offset: Mapped[str] = mapped_column(String(200))
    schema_version: Mapped[str] = mapped_column(String(50))
    data_version_id: Mapped[int | None] = mapped_column(ForeignKey("data_version.id"), index=True)
    processed_at: Mapped[datetime] = _ts()


class CdcOffset(Base):
    """How far each consumer has read, committed in the same transaction as the events."""

    __tablename__ = "cdc_offset"
    consumer: Mapped[str] = mapped_column(String(200), primary_key=True)
    position: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_log_is_append_only(_mapper, _connection, target: AuditLog) -> None:
    raise PermissionError(f"audit_log is append-only (row {target.id})")
