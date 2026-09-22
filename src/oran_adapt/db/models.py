"""PostgreSQL data model.

PostgreSQL owns datasets, data versions, lineage, model<->data links, jobs and audit.
It deliberately does NOT own model versions or artifacts: MLflow is the only authority for
those. The `model_version` columns below are *references* to MLflow versions, not a registry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    ingested_at: Mapped[datetime] = _ts()
    dataset: Mapped[DatasetMetadata] = relationship(back_populates="versions")
    records: Mapped[list[DataRecord]] = relationship(back_populates="data_version")


class DataRecord(Base):
    """One observation row of a data version (feature/target payload with its timestamp)."""

    __tablename__ = "data_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    data_version_id: Mapped[int] = mapped_column(ForeignKey("data_version.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
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


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    component: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str | None] = mapped_column(String(200))
    model_version: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="OK")
    detail: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
