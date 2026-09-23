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
    # Written by releases before Phase 14; kept so older job rows still load. Never entered now.
    ANALYZING = "ANALYZING"
    MODEL_COMPARISON = "MODEL_COMPARISON"
    DECISION_MADE = "DECISION_MADE"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.ROLLED_BACK})


class Strategy(StrEnum):
    FINE_TUNING = "FINE_TUNING"
    FULL_RETRAINING = "FULL_RETRAINING"
    ROLLBACK = "ROLLBACK"
    NO_ACTION = "NO_ACTION"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    NO_COMPATIBLE_STRATEGY = "NO_COMPATIBLE_STRATEGY"


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
