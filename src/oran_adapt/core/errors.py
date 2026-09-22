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


class JobTimeoutError(AdaptationError):
    """Raised by the Phase 10 job wrapper when a job's total wall-clock budget
    (``Settings.job_timeout_s``) is exceeded. Python cannot forcibly kill the worker thread that
    was running the pipeline, so it is left to finish on its own, detached from the caller."""

    code = "JOB_TIMEOUT"
