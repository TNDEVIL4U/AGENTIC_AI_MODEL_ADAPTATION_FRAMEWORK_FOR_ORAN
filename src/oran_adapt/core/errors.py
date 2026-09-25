"""Structured exceptions. Each carries a stable code so failures are never silent."""


class AdaptationError(Exception):
    code = "ADAPTATION_ERROR"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigurationError(AdaptationError):
    code = "CONFIGURATION_ERROR"


class RegistryUnavailableError(AdaptationError):
    code = "MLFLOW_UNAVAILABLE"


class DatabaseUnavailableError(AdaptationError):
    code = "DATABASE_UNAVAILABLE"


class ModelNotFoundError(AdaptationError):
    code = "MODEL_NOT_FOUND"


class ConflictError(AdaptationError):
    """The request clashes with something already recorded (e.g. a model_id that is already
    onboarded, or a data version name reused for different content)."""

    code = "CONFLICT"


class DataVersionConflictError(ConflictError):
    code = "DATA_VERSION_CONFLICT"


class DatasetNotFoundError(AdaptationError):
    code = "DATASET_NOT_FOUND"


class ArtifactError(AdaptationError):
    code = "ARTIFACT_ERROR"


class UnsupportedAdaptationError(AdaptationError):
    code = "ADAPTATION_UNSUPPORTED"


class ValidationFailedError(AdaptationError):
    code = "VALIDATION_FAILED"


class LlmUnavailableError(AdaptationError):
    code = "LLM_UNAVAILABLE"


class UnsafeCodeError(AdaptationError):
    """Raised when LLM-generated adaptation code fails the sandbox's static AST security scan.
    The code is rejected before it is ever executed."""

    code = "UNSAFE_CODE_REJECTED"


class SandboxExecutionError(AdaptationError):
    """Raised when security-checked code fails during actual sandbox execution: it timed out,
    exceeded its resource limits, exited non-zero, or produced no usable result."""

    code = "SANDBOX_EXECUTION_FAILED"


class ArtifactIntegrityError(ArtifactError):
    """A model artifact's SHA-256 no longer matches the checksum recorded when it was
    registered. The artifact is refused, never loaded."""

    code = "ARTIFACT_INTEGRITY_FAILED"


class ModelBusyError(ConflictError):
    """Another adaptation job holds this model's lock. Nothing was recorded; the caller can
    resend the same event later."""

    code = "MODEL_BUSY"


class InvalidTransitionError(AdaptationError):
    code = "INVALID_STATE_TRANSITION"


class PromotionError(AdaptationError):
    """Moving the live alias failed. LIVE was restored to the version it pointed at before."""

    code = "PROMOTION_FAILED"


class CdcUnavailableError(AdaptationError):
    """The CDC source (Kafka, or its client library) cannot be reached. Nothing was consumed
    and no offset moved, so the next run picks up where the last one stopped."""

    code = "CDC_UNAVAILABLE"


class CdcProcessingError(AdaptationError):
    """A CDC batch could not be stored. The transaction was rolled back and the source offset
    not acknowledged, so the batch is redelivered (and deduplicated) on the next run."""

    code = "CDC_PROCESSING_FAILED"


class DataLeakageError(AdaptationError):
    """The training data would leak the evaluation target or the held-out rows into the model
    (e.g. the target is also a feature). Nothing was trained."""

    code = "DATA_LEAKAGE"


class JobTimeoutError(AdaptationError):
    """Raised by the Phase 10 job wrapper when a job's total wall-clock budget
    (``Settings.job_timeout_s``) is exceeded. In the default process mode the job's worker
    process is terminated, then killed; the job is recorded TIMED_OUT. In thread mode (tests
    only) the worker thread cannot be stopped and is left to finish, detached from the caller."""

    code = "JOB_TIMEOUT"


class JobAbandonedError(AdaptationError):
    """Recorded (never raised) on a job whose process died - a crash or a service restart -
    before the job finished. Its model lock expired and a newer job took it over, which marks
    the old job FAILED with this error so it does not stay in a running state forever."""

    code = "JOB_ABANDONED"
