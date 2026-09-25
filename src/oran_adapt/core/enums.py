"""Shared enumerations for the adaptation pipeline."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Adaptation job states. Allowed moves between them live in core.state_machine."""

    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"  # the inbound event is being checked
    DATA_PREPARING = "DATA_PREPARING"
    EVALUATING_VERSIONS = "EVALUATING_VERSIONS"
    REUSE_DECISION = "REUSE_DECISION"
    DECISION_PENDING = "DECISION_PENDING"
    ADAPTING = "ADAPTING"
    VALIDATING_CANDIDATE = "VALIDATING_CANDIDATE"
    REGISTERING = "REGISTERING"
    PROMOTING = "PROMOTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    # The job ran past Settings.job_timeout_s and its worker process was killed.
    TIMED_OUT = "TIMED_OUT"
    # Written by releases before Phase 14; kept so older job rows still load. Never entered now.
    ANALYZING = "ANALYZING"
    MODEL_COMPARISON = "MODEL_COMPARISON"
    DECISION_MADE = "DECISION_MADE"


TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.ROLLED_BACK, JobStatus.TIMED_OUT}
)


class Strategy(StrEnum):
    FINE_TUNING = "FINE_TUNING"
    FULL_RETRAINING = "FULL_RETRAINING"
    ROLLBACK = "ROLLBACK"
    NO_ACTION = "NO_ACTION"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    NO_COMPATIBLE_STRATEGY = "NO_COMPATIBLE_STRATEGY"


class TaskType(StrEnum):
    """What a model predicts; picks the metric set it is scored with (validation.metrics)."""

    CLASSIFICATION = "CLASSIFICATION"
    REGRESSION = "REGRESSION"
    FORECASTING = "FORECASTING"
    CLUSTERING = "CLUSTERING"
    ANOMALY_DETECTION = "ANOMALY_DETECTION"


class ReuseVerdict(StrEnum):
    """Member 1's answer after scoring every registered version on the current data."""

    REUSE_EXISTING_VERSION = "REUSE_EXISTING_VERSION"
    ADAPT_MODEL = "ADAPT_MODEL"
    RETRAIN_MODEL = "RETRAIN_MODEL"


class ModelVersionStatus(StrEnum):
    """Lifecycle status kept on each MLflow model version as the ``oran.status`` tag. Aliases
    (``live``, ``candidate``) point at one version each; this tag is the per-version history."""

    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    LIVE = "LIVE"
    ARCHIVED = "ARCHIVED"


class PromotionKind(StrEnum):
    PROMOTE_CANDIDATE = "PROMOTE_CANDIDATE"
    REUSE = "REUSE"
    ROLLBACK = "ROLLBACK"


class DataKind(StrEnum):
    HISTORICAL = "HISTORICAL"
    DRIFTED = "DRIFTED"
    CDC = "CDC"  # net row changes captured by CDC since the previous CDC version
    CURRENT = "CURRENT"  # the cleaned rows one adaptation job evaluated on (CurrentData)


class CdcOperation(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class AssociationRole(StrEnum):
    TRAINING = "TRAINING"
    VALIDATION = "VALIDATION"
    DRIFT_OBSERVED = "DRIFT_OBSERVED"


class EngineKind(StrEnum):
    SKLEARN_PARTIAL_FIT = "SKLEARN_PARTIAL_FIT"
    SKLEARN_FULL_RETRAIN = "SKLEARN_FULL_RETRAIN"
    XGBOOST_FULL_RETRAIN = "XGBOOST_FULL_RETRAIN"
    TORCH_FINE_TUNE = "TORCH_FINE_TUNE"
    TORCH_FULL_RETRAIN = "TORCH_FULL_RETRAIN"
    LLM_GENERATED = "LLM_GENERATED"


class Role(StrEnum):
    """API caller roles. Every role can read; writes need the roles each endpoint names."""

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    ML_ENGINEER = "ML_ENGINEER"
    READ_ONLY = "READ_ONLY"


class AuditAction(StrEnum):
    """The audit events the platform records (append-only audit_log rows)."""

    DRIFT_RECEIVED = "DRIFT_RECEIVED"
    DATA_VERSION_CREATED = "DATA_VERSION_CREATED"
    CURRENT_DATA_CREATED = "CURRENT_DATA_CREATED"
    MODEL_VERSION_EVALUATED = "MODEL_VERSION_EVALUATED"
    MODEL_REUSE_SELECTED = "MODEL_REUSE_SELECTED"
    ADAPTATION_DECISION_CREATED = "ADAPTATION_DECISION_CREATED"
    FINE_TUNE_STARTED = "FINE_TUNE_STARTED"
    RETRAIN_STARTED = "RETRAIN_STARTED"
    ADAPTER_GENERATED = "ADAPTER_GENERATED"
    SANDBOX_EXECUTED = "SANDBOX_EXECUTED"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MODEL_REGISTERED = "MODEL_REGISTERED"
    MODEL_PROMOTED = "MODEL_PROMOTED"
    MODEL_ROLLED_BACK = "MODEL_ROLLED_BACK"
