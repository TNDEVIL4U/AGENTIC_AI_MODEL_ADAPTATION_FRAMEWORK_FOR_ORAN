"""Typed, layered, schema-validated configuration.

Layers (highest first): keyword arguments, environment, ``.env``, the secrets backend
(SECRETS_BACKEND), the TOML file named by ORAN_CONFIG_FILE, then the defaults below - see
core.config_sources. Every key is declared here with its type and bounds; nothing else in the
code base supplies a default of its own.

Startup fails fast: ``load_settings()`` turns any validation failure into a ConfigurationError
that names each offending key, and every selected adapter's required keys must be set. In the
``production`` environment the storage locations must be given explicitly, never defaulted.
``oran-adapt config lint FILE`` checks a config file the same way without starting anything.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, ClassVar, Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from oran_adapt.core.config_sources import (
    SecretsSource,
    TomlFileSource,
    is_secret_field,
    read_config_file,
)
from oran_adapt.core.errors import ConfigurationError

# Adapter selector key -> port. Each selected adapter's Capability.required_keys must be set.
ADAPTER_SELECTORS: dict[str, str] = {
    "registry_backend": "registry",
    "llm_provider": "llm",
    "job_execution_mode": "job_executor",
    "cdc_mode": "cdc_source",
    "auth_backend": "auth",
    "policy_backend": "policy",
    "notification_backend": "notification",
    "secrets_backend": "secrets",
}
# Selector values that mean "this port is switched off" rather than naming an adapter.
DISABLED = {"llm_provider": "none", "cdc_mode": "disabled"}
# Keys a production deployment must set explicitly (a default would point at a local file).
PRODUCTION_REQUIRED = ("database_url", "mlflow_tracking_uri", "artifact_workdir")

_ALL_ROLES = ["ADMIN", "OPERATOR", "ML_ENGINEER", "READ_ONLY"]


class Settings(BaseSettings):
    # Environment variables the schema does not name are ignored (the environment holds far more
    # than this application's keys); unknown keys in a config file are an error instead.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # True only for the file lint: secret-typed keys then count as supplied from elsewhere.
    _secrets_external: ClassVar[bool] = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        file_source = TomlFileSource(settings_cls)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            SecretsSource(settings_cls, file_source),
            file_source,
        )

    # "production" refuses defaulted storage locations (PRODUCTION_REQUIRED).
    environment: Literal["development", "production"] = "development"

    database_url: str = "sqlite:///./data/oran_adapt.db"
    mlflow_tracking_uri: str = "sqlite:///./data/mlflow.db"
    mlflow_registry_uri: str | None = None
    artifact_workdir: str = "./data/artifacts"

    # Model registry adapter (entry point group oran_adapt.registry).
    registry_backend: str = "mlflow"
    # MLflow client HTTP behaviour, applied to MLFLOW_HTTP_REQUEST_* unless those are set.
    mlflow_http_max_retries: int = Field(1, ge=0)
    mlflow_http_backoff_factor: float = Field(0.0, ge=0)
    mlflow_http_timeout_s: float = Field(10.0, gt=0)
    # Registry tag names the framework writes on model versions.
    registry_tags_checksum: str = Field("artifact.sha256", min_length=1)
    registry_tags_status: str = Field("oran.status", min_length=1)

    # LLM adapter (oran_adapt.llm group) or "none" for the deterministic path only.
    llm_provider: str = "none"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    llm_timeout_s: float = Field(60.0, gt=0)
    llm_max_output_tokens: int = Field(2048, ge=1)
    # Provider SDK retries on top of the first attempt (0: fail fast, the job retries instead).
    llm_max_retries: int = Field(0, ge=0)

    sandbox_backend: Literal["docker", "subprocess"] = "subprocess"
    sandbox_timeout_s: int = Field(120, gt=0)
    sandbox_memory_mb: int = Field(1024, gt=63)
    sandbox_docker_image: str = "oran-adapt-sandbox:latest"
    sandbox_docker_pids_limit: int = Field(128, ge=16)
    sandbox_docker_cpus: float = Field(1.0, gt=0)
    sandbox_docker_tmpfs_mb: int = Field(64, ge=1)
    sandbox_docker_cleanup_timeout_s: float = Field(60.0, gt=0)
    # Largest result manifest a sandboxed run may write.
    sandbox_manifest_max_bytes: int = Field(4096, ge=256)

    live_alias: str = "live"
    # Points at the newest registered, validated candidate (whether or not it went live).
    candidate_alias: str = "candidate"
    log_level: str = "INFO"
    log_json: bool = True

    # The job wrapper retries a job up to job_max_retries times (exponential backoff starting at
    # job_retry_backoff_s) on a transient error (registry or database unreachable), and gives the
    # whole pipeline run at most job_timeout_s wall-clock seconds before recording it TIMED_OUT.
    job_max_retries: int = Field(2, ge=0)
    job_retry_backoff_s: float = Field(1.0, ge=0)
    job_timeout_s: float = Field(600.0, gt=0)
    # Job executor adapter (oran_adapt.job_executor): "process" runs each attempt in its own
    # worker process, killed on a timeout; "thread" runs it in this process and cannot be
    # stopped - only for tests or debugging that inject in-process fakes.
    job_execution_mode: str = "process"
    # How long a terminated worker gets to exit before it is killed outright.
    job_kill_grace_s: float = Field(5.0, gt=0)

    # Member 1 (analysis) reuse thresholds. A feature is treated as "shifted" once its PSI
    # crosses analysis_psi_reuse_threshold OR its KS test p-value drops below
    # analysis_ks_pvalue_reuse_threshold; a caller-supplied drift_score at or above
    # analysis_drift_score_reuse_threshold forces non-reuse outright.
    analysis_psi_reuse_threshold: float = Field(0.1, ge=0)
    analysis_ks_pvalue_reuse_threshold: float = Field(0.05, gt=0, lt=1)
    analysis_drift_score_reuse_threshold: float = Field(0.3, ge=0, le=1)
    # PSI alone counts a feature as shifted only with at least this many rows in both segments;
    # below it, only a significant KS test does (PSI on a few rows is noise).
    analysis_min_psi_rows: int = Field(30, ge=2)
    # How many past evaluations the analysis summary reports per model.
    analysis_performance_history_limit: int = Field(10, ge=1)

    # Member 1 historical version reuse. Once drift is confirmed, every registered version (the
    # newest reuse_max_versions of them) is scored on the held-out newest drifted rows. A non-live
    # version is reused, instead of training anything, when it beats LIVE there by at least
    # reuse_min_accuracy_gain (classifiers, absolute) or reuse_min_rmse_reduction_ratio
    # (regressors, a fraction of LIVE's RMSE), and is no older than reuse_max_model_age_days
    # when that is set. reuse_confidence_rows is how many scored rows count as full confidence.
    reuse_enabled: bool = True
    reuse_max_versions: int = Field(10, ge=1)
    reuse_min_accuracy_gain: float = Field(0.02, ge=0, le=1)
    reuse_min_rmse_reduction_ratio: float = Field(0.05, ge=0, lt=1)
    reuse_max_model_age_days: float | None = Field(None, gt=0)
    reuse_confidence_rows: int = Field(100, ge=1)

    # A job holds its model's lock while it runs so two drift events cannot both move LIVE. A
    # lock older than this is treated as abandoned (e.g. the process died) and can be taken over.
    model_lock_ttl_s: float = Field(3600.0, gt=0)

    # Member 2 (decision) hard constraints. A DecisionPackage needs at least
    # decision_min_drifted_rows drifted rows to be actionable at all; a framework outside
    # decision_supported_frameworks has no adaptation engine to carry a decision out; a max_psi
    # at or above decision_full_retrain_psi_threshold rules out incremental fine-tuning.
    decision_min_drifted_rows: int = Field(10, ge=1)
    decision_full_retrain_psi_threshold: float = Field(0.5, ge=0)
    decision_supported_frameworks: list[str] = Field(
        default_factory=lambda: ["sklearn", "xgboost", "torch", "pytorch"]
    )
    # How many copies of a past decision the decision memory keeps per model and strategy.
    decision_memory_copies: int = Field(3, ge=1)
    # Confidence reported for a rule-based decision that carries no score of its own.
    decision_default_confidence: float = Field(0.5, ge=0, le=1)

    # Member 4 (validation) pass/fail gate. A candidate needs at least validation_min_rows rows
    # of held-out data to be scored at all. A classifier candidate passes when its accuracy is no
    # more than validation_accuracy_tolerance below V_current's; a regressor candidate passes
    # when its RMSE is no more than validation_rmse_tolerance_ratio (a fraction of V_current's
    # RMSE, since RMSE has no fixed scale) higher than V_current's.
    # The hold-out is the newest validation_holdout_fraction of the drifted rows (at least
    # validation_min_rows of them, always leaving one drifted row to train on). Those rows are
    # never trained on, so both models are scored on data neither has seen.
    validation_min_rows: int = Field(5, ge=1)
    validation_holdout_fraction: float = Field(0.2, gt=0, lt=1)
    validation_accuracy_tolerance: float = Field(0.02, ge=0, le=1)
    validation_rmse_tolerance_ratio: float = Field(0.05, ge=0)

    # Leakage checks run on the training rows before any engine fits (adaptation.leakage).
    # Training rows that are held-out rows, copies of them, or newer than the oldest held-out
    # row are dropped; a feature that is the target (by name or identical values) fails the job.
    # leakage_target_correlation_max, when set, also fails a numeric feature whose absolute
    # correlation with the target reaches it. leakage_allow_future_rows keeps newer rows, for
    # data that is not a time series.
    leakage_checks_enabled: bool = True
    leakage_allow_future_rows: bool = False
    leakage_target_correlation_max: float | None = Field(None, gt=0, le=1)

    # MLflow >= 3 serializes sklearn models with skops, which refuses to save or load any type
    # not on its built-in safe list. These are the extra types the framework has reviewed and
    # trusts - the tree node stores behind DecisionTree*/RandomForest*/ExtraTrees*/
    # GradientBoosting*/IsolationForest and HistGradientBoosting*. A model needing any other
    # untrusted type is refused with ARTIFACT_ERROR instead of being trusted blindly.
    mlflow_skops_trusted_types: list[str] = Field(
        default_factory=lambda: [
            "sklearn.tree._tree.Tree",
            "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
        ]
    )

    # Largest model artifact (file or directory) downloaded, loaded or registered; a bigger one is
    # refused with ARTIFACT_ERROR before it is deserialized.
    artifact_max_bytes: int = Field(2 * 1024**3, ge=1024)
    # Read size when hashing artifacts for their integrity checksum.
    artifact_hash_chunk_bytes: int = Field(1024 * 1024, ge=4096)

    # Torch engine budgets (full-batch Adam steps). A from-scratch retrain needs far more steps
    # than a warm-start fine-tune to get back to the current model's quality.
    torch_fine_tune_epochs: int = Field(5, ge=1)
    torch_full_retrain_epochs: int = Field(300, ge=1)
    torch_learning_rate: float = Field(1e-2, gt=0)  # Adam step size for both torch engines

    # Largest request body the API accepts (dataset uploads send their records as JSON); a
    # bigger one is refused with 413 REQUEST_TOO_LARGE before it is read in full.
    api_max_request_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    api_pagination_default_limit: int = Field(50, ge=1, le=1000)
    # Request/response header carrying the correlation id.
    api_correlation_header: str = Field("X-Correlation-ID", min_length=1)

    # API authentication (auth_backend, oran_adapt.auth) and authorization (policy_backend,
    # oran_adapt.policy). With the api-key backend, callers send the key in auth_api_key_header
    # (or ``Authorization: Bearer <key>``). Keys are never stored: API_KEYS maps the SHA-256 hex
    # digest of each key to "ROLE" or "ROLE:caller-name" (`oran-adapt auth new-key` makes a key
    # and its entry). With auth enabled and no keys configured, every protected endpoint refuses
    # (fail closed). /health, /readiness and, with metrics_public, /metrics need no key.
    auth_enabled: bool = True
    auth_backend: str = "api-key"
    auth_api_key_header: str = Field("X-API-Key", min_length=1)
    api_keys: dict[str, str] = Field(default_factory=dict)
    metrics_public: bool = True
    policy_backend: str = "static-rbac"
    # Action -> roles allowed to do it. An action missing here is allowed to nobody.
    policy_roles: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "read": list(_ALL_ROLES),
            "submit": ["ADMIN", "OPERATOR", "ML_ENGINEER"],
            "data": ["ADMIN", "ML_ENGINEER"],
            "promote": ["ADMIN", "OPERATOR"],
            "admin": ["ADMIN"],
        }
    )

    # Notifications on job outcomes (oran_adapt.notification).
    notification_backend: str = "log"
    notification_webhook_url: str | None = None
    notification_webhook_timeout_s: float = Field(5.0, gt=0)

    # Where secret-typed keys (API keys of providers) come from besides the environment
    # (oran_adapt.secrets): "env" or "file" (one file per secret in secrets_dir).
    secrets_backend: str = "env"
    secrets_dir: str | None = None

    # Change data capture from the source table (see docs/CDC.md): "disabled", or a cdc_source
    # adapter - "kafka" (Debezium via Kafka, production) or "polling" (trigger-fed changelog).
    cdc_mode: str = "disabled"
    cdc_batch_size: int = Field(500, ge=1)
    cdc_polling_table: str = Field("kpi_sample", min_length=1)
    cdc_schema_ref: str = Field("kpi_sample/1", min_length=1)
    cdc_idle_poll_s: float = Field(1.0, gt=0)
    # A data version records at most this many source transaction ids (lineage).
    cdc_max_tx_ids_per_version: int = Field(1000, ge=1)
    kafka_bootstrap_servers: str | None = None
    cdc_kafka_topic: str = "oran.public.kpi_sample"  # Debezium: <prefix>.<schema>.<table>
    cdc_consumer_group: str = "oran-adapt-cdc"
    cdc_kafka_poll_timeout_s: float = Field(1.0, gt=0)
    cdc_kafka_auto_offset_reset: Literal["earliest", "latest"] = "earliest"

    @model_validator(mode="after")
    def _api_keys_well_formed(self) -> Settings:
        from oran_adapt.core.enums import Role

        for digest, spec in self.api_keys.items():
            if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
                raise ValueError("API_KEYS keys must be SHA-256 hex digests of the API keys")
            if spec.partition(":")[0] not in Role.__members__:
                raise ValueError(f"API_KEYS role must be one of {', '.join(Role)}")
        return self

    @model_validator(mode="after")
    def _production_explicit(self) -> Settings:
        if self.environment == "production":
            missing = [k for k in PRODUCTION_REQUIRED if k not in self.model_fields_set]
            if missing:
                raise ConfigurationError(
                    "ENVIRONMENT=production requires "
                    + ", ".join(k.upper() for k in missing)
                    + " to be set explicitly",
                    key=missing[0].upper(),
                    missing=[k.upper() for k in missing],
                )
        return self

    @model_validator(mode="after")
    def _adapter_keys_present(self) -> Settings:
        """Every selected adapter exists and has its required keys set."""
        from oran_adapt import plugins

        for selector, port in ADAPTER_SELECTORS.items():
            name = getattr(self, selector)
            if DISABLED.get(selector) == name:
                continue
            spec = plugins.resolve(port, name, config_key=selector)
            for key in spec.capability.required_keys:
                if self._secrets_external and is_secret_field(type(self).model_fields[key]):
                    continue
                value = getattr(self, key)
                if value is None or value == "" or value == [] or value == {}:
                    raise ConfigurationError(
                        f"{selector.upper()}={name} requires {key.upper()}",
                        key=key.upper(),
                        selected_by=selector.upper(),
                    )
        return self


def _problems(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {"key": ".".join(str(p) for p in err["loc"]).upper() or "SETTINGS", "error": err["msg"]}
        for err in exc.errors()
    ]


def _validation_error(exc: ValidationError) -> ConfigurationError:
    problems = _problems(exc)
    keys = ", ".join(p["key"] for p in problems)
    return ConfigurationError(
        f"invalid configuration: {keys}: {problems[0]['error']}",
        key=problems[0]["key"],
        problems=problems,
    )


def load_settings(**overrides: Any) -> Settings:
    """Settings from every layer; any failure is a ConfigurationError naming the key(s)."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise _validation_error(exc) from None


class _FileOnlySettings(Settings):
    """Settings from keyword arguments and defaults only (no environment, .env or secrets).
    Secret-typed keys are expected from the environment or a secrets backend, never a file."""

    _secrets_external: ClassVar[bool] = True

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def lint_config_file(path: str) -> list[str]:
    """Problems with one config file on its own (not merged with the environment, ``.env`` or a
    secrets backend); [] if none."""
    try:
        _FileOnlySettings(**read_config_file(Settings, path))
    except ConfigurationError as exc:
        return [exc.message if str(path) in exc.message else f"{path}: {exc.message}"]
    except ValidationError as exc:
        return [f"{path}: {p['key']}: {p['error']}" for p in _problems(exc)]
    return []


@lru_cache
def get_settings() -> Settings:
    return load_settings()
