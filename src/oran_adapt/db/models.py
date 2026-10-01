"""PostgreSQL data model.

PostgreSQL owns datasets, data versions, lineage, model<->data links, jobs and audit.
It deliberately does NOT own model versions or artifacts: MLflow is the only authority for
those. The `model_version` columns below are *references* to MLflow versions, not a registry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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
    __table_args__ = (Index("ix_adaptation_job_claim", "status", "worker_class", "available_at"),)
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
    # The job queue (orchestrator.queue): who may run the job, in what order, and who runs it
    # now. A worker claims a QUEUED job whose available_at has passed by writing a fresh
    # lease_token; every later write of that worker is fenced on the token.
    tenant: Mapped[str] = mapped_column(String(100), default="default", index=True)
    worker_class: Mapped[str] = mapped_column(String(50), default="default")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_by: Mapped[str | None] = mapped_column(String(200))
    # Attempts that ended without an outcome (the worker died or lost its lease); at
    # JOB_POISON_THRESHOLD the job is quarantined instead of being run again.
    lost_count: Mapped[int] = mapped_column(Integer, default=0)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # W3C traceparent of the intake span (core.tracing): the worker continues this trace.
    trace_context: Mapped[str | None] = mapped_column(String(128))
    events: Mapped[list[AdaptationEvent]] = relationship(back_populates="job")


class JobSlot(Base):
    """One of a tenant's concurrency slots (JOB_TENANT_CONCURRENCY / JOB_TENANT_LIMITS), held by
    the job running in it. The primary key makes the limit exact across workers: two claims of
    the same slot cannot both commit. ``expires_at`` is renewed with the job's lease, so a slot
    whose worker died frees itself."""

    __tablename__ = "job_slot"
    tenant: Mapped[str] = mapped_column(String(100), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class NotificationEvent(Base):
    """The outbox (oran_adapt.notifications): one row per event, written in the transaction
    that made the change it reports, so an event exists if and only if its change committed.
    ``envelope`` is the CloudEvents JSON sent to every sink; ``event_id`` is its ``id``."""

    __tablename__ = "notification_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    subject: Mapped[str] = mapped_column(String(200), index=True)  # the job id
    model_id: Mapped[str | None] = mapped_column(String(200), index=True)
    envelope: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()
    deliveries: Mapped[list[NotificationDelivery]] = relationship(back_populates="event")


class NotificationDelivery(Base):
    """One event's delivery to one sink. PENDING until the sink takes it (DELIVERED) or it runs
    out of attempts or is refused for good (DEAD, until redriven). A dispatcher claims a row by
    setting ``lease_until``; a row whose lease has run out is claimable again."""

    __tablename__ = "notification_delivery"
    __table_args__ = (UniqueConstraint("event_id", "sink"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("notification_event.event_id"), index=True
    )
    sink: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    redrive_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event: Mapped[NotificationEvent] = relationship(back_populates="deliveries")


class GateDecisionRecord(Base):
    """One decision of the validation gate (validation.gate): the verdict, the policy version
    and hash it was made under, and the full GateDecision in ``decision``."""

    __tablename__ = "gate_decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    current_version: Mapped[str | None] = mapped_column(String(50))
    candidate_version: Mapped[str | None] = mapped_column(String(50))
    verdict: Mapped[str] = mapped_column(String(10), index=True)  # ACCEPT | REJECT
    metric: Mapped[str] = mapped_column(String(50))
    policy_version: Mapped[str] = mapped_column(String(50))
    policy_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()


class Rollout(Base):
    """A validated candidate on its way to traffic (delivery.controller). ``state`` moves
    SHADOW / CANARY / AB / AWAITING_APPROVAL -> PROMOTED | ROLLED_BACK | EXPIRED | REJECTED;
    ``history`` lists every transition with its reason. ``policy`` is the DeliveryPolicy the
    rollout started under - later config changes do not alter a running rollout."""

    __tablename__ = "rollout"
    id: Mapped[int] = mapped_column(primary_key=True)
    rollout_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), index=True)
    stable_version: Mapped[str | None] = mapped_column(String(50))
    candidate_version: Mapped[str] = mapped_column(String(50))
    step: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    policy: Mapped[dict] = mapped_column(JSON)
    policy_hash: Mapped[str] = mapped_column(String(64))
    gate_decision_id: Mapped[int | None] = mapped_column(Integer)
    step_started_at: Mapped[datetime] = _ts()
    # When the current state times out (shadow_max_s, ab_duration_s, approval_ttl_s).
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text, default="")
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RolloutObservation(Base):
    """Online metrics of one arm (stable | candidate) of a rollout over some requests, as
    submitted to the API (the ``api`` rollout metrics adapter reads these)."""

    __tablename__ = "rollout_observation"
    id: Mapped[int] = mapped_column(primary_key=True)
    rollout_id: Mapped[str] = mapped_column(String(64), index=True)
    arm: Mapped[str] = mapped_column(String(20))
    requests: Mapped[int] = mapped_column(Integer, default=1)
    metrics: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class LlmUsage(Base):
    """One LLM call's usage, the ledger the token and cost budgets are checked against
    (llm.guard). Rows are written after each call that reached the provider."""

    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    prompt_id: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str] = mapped_column(String(40), default="ok")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_log_is_append_only(_mapper, _connection, target: AuditLog) -> None:
    raise PermissionError(f"audit_log is append-only (row {target.id})")
