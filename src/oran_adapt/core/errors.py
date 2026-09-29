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


class InvalidReferenceError(AdaptationError):
    """A model URI, model name, version or alias that is malformed (not merely absent)."""

    code = "INVALID_REFERENCE"


class DatasetNotFoundError(AdaptationError):
    code = "DATASET_NOT_FOUND"


class ArtifactError(AdaptationError):
    code = "ARTIFACT_ERROR"


class DataSourceNotAllowedError(AdaptationError):
    """A data reference names a scheme with no enabled dataset adapter, or a location outside
    the adapter's allow-list (DATASET_FILE_ROOTS, DATASET_HTTP_ALLOWED_HOSTS, ...)."""

    code = "DATA_SOURCE_NOT_ALLOWED"


class DataSourceUnavailableError(AdaptationError):
    """The storage a data reference points at could not be reached (network, timeout, 5xx)."""

    code = "DATA_SOURCE_UNAVAILABLE"


class DataSourceChangedError(ConflictError):
    """The object behind a by-reference data version is no longer the one registered: its
    fingerprint or content hash differs. A data version is immutable, so it is not read."""

    code = "DATA_SOURCE_CHANGED"


class DataFormatError(ArtifactError):
    """Referenced data could not be parsed in its declared format, or its columns change
    type in a way no single column type holds."""

    code = "DATA_FORMAT_INVALID"


class DataTooLargeError(AdaptationError):
    """Materializing the data would pass a memory ceiling (DATASET_MAX_ROWS,
    DATASET_MAX_SOURCE_BYTES). Refused before anything is read, never an out-of-memory crash."""

    code = "DATA_TOO_LARGE"


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


class DeploymentError(AdaptationError):
    """A serving system refused a deployment, or did not read back as serving the version asked
    for within DEPLOYMENT_TIMEOUT_S. ``restored`` in the context says whether the previous
    version was put back and read back."""

    code = "DEPLOYMENT_FAILED"


class DeploymentUnavailableError(AdaptationError):
    """The serving system (or its control plane) cannot be reached."""

    code = "DEPLOYMENT_UNAVAILABLE"


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


class JobNotFoundError(AdaptationError):
    code = "JOB_NOT_FOUND"


class JobCancelledError(AdaptationError):
    """The job was cancelled on request. Raised at the job's next checkpoint (a stage report
    or a supervisor tick) after the request, and recorded as the CANCELLED job's error."""

    code = "JOB_CANCELLED"


class JobLeaseLostError(AdaptationError):
    """This worker no longer holds the job's lease: it stopped renewing it in time and the
    reaper handed the job on. Everything the worker would still write is refused, so a job is
    never run to an outcome twice."""

    code = "JOB_LEASE_LOST"


class JobDrainedError(AdaptationError):
    """The worker was shutting down and the job did not finish within JOB_DRAIN_TIMEOUT_S: its
    attempt was stopped and the job put back in the queue for another worker."""

    code = "JOB_DRAINED"


class JobQuarantinedError(AdaptationError):
    """Recorded (never raised) on a poison job: JOB_POISON_THRESHOLD of its attempts ended
    without an outcome (the worker died or lost its lease), so it is not run again."""

    code = "JOB_QUARANTINED"


class JobQueueUnavailableError(AdaptationError):
    """The job queue's broker could not be reached. The job stays QUEUED in the database and
    the reaper publishes it again."""

    code = "JOB_QUEUE_UNAVAILABLE"


class JobNotCancellableError(ConflictError):
    """The job already ended, or is registering or promoting (past the point where stopping it
    would leave the registry consistent)."""

    code = "JOB_NOT_CANCELLABLE"


class AuthenticationError(AdaptationError):
    """No valid credential was presented (HTTP 401)."""

    code = "UNAUTHENTICATED"


class PermissionDeniedError(AdaptationError):
    """The authenticated caller's role may not perform the action (HTTP 403)."""

    code = "FORBIDDEN"


class NotificationDeliveryError(AdaptationError):
    """A notification sink did not take a message. ``retryable`` in the context says whether
    sending it again later may succeed (the receiver was down, timed out, answered 5xx or 429)
    or not (it rejected the message: bad signature, bad request). Raised by sink adapters to
    the dispatcher only, never into the job that produced the event."""

    code = "NOTIFICATION_DELIVERY_FAILED"

    def __init__(self, message: str, *, retryable: bool, **context: object) -> None:
        super().__init__(message, retryable=retryable, **context)
        self.retryable = retryable


class DeliveryNotFoundError(AdaptationError):
    code = "DELIVERY_NOT_FOUND"


class SignatureVerificationError(AdaptationError):
    """A notification's signature headers are missing, stale, or match none of the keys
    (oran_adapt.notifications.signing.verify): the receiver must reject the message."""

    code = "INVALID_SIGNATURE"


class JobWorkerError(AdaptationError):
    """A job worker died or crashed without reporting an AdaptationError. Same code as any other
    unexpected failure, so the recorded job error is unchanged by the execution mode."""

    code = "INTERNAL_ERROR"


class JobWorkerLostError(JobWorkerError):
    """The job's worker process died without reporting anything (killed, out of memory). The
    attempt had no outcome, so the job is retried; a job that keeps killing its workers is
    quarantined (JOB_POISON_THRESHOLD)."""

    code = "JOB_WORKER_LOST"


class RolloutNotFoundError(AdaptationError):
    code = "ROLLOUT_NOT_FOUND"


class RolloutStateError(ConflictError):
    """The rollout is not in a state that allows the request (e.g. approving a rollout that is
    not awaiting approval, or one that already ended)."""

    code = "ROLLOUT_STATE_CONFLICT"


class RolloutMetricsUnavailableError(AdaptationError):
    """The rollout metrics source (Prometheus, ...) cannot be reached. The rollout stays where
    it is and is ticked again later; it never advances on missing evidence."""

    code = "ROLLOUT_METRICS_UNAVAILABLE"


def rebuild_error(code: str, message: str, context: dict[str, object]) -> AdaptationError:
    """An AdaptationError sent across a process boundary as (code, message, context), rebuilt
    with its original class so ``except`` clauses and the recorded code behave the same."""
    stack: list[type[AdaptationError]] = [AdaptationError]
    while stack:
        cls = stack.pop()
        if cls.code == code:
            try:
                return cls(message, **context)
            except TypeError:
                break
        stack.extend(cls.__subclasses__())
    err = AdaptationError(message, **context)
    err.code = code
    return err
