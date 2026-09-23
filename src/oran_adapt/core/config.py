"""Application configuration, loaded from environment / .env (never hard-coded secrets)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/oran_adapt.db"
    mlflow_tracking_uri: str = "sqlite:///./data/mlflow.db"
    mlflow_registry_uri: str | None = None
    artifact_workdir: str = "./data/artifacts"

    llm_provider: Literal["anthropic", "gemini", "none"] = "none"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    llm_timeout_s: float = Field(60.0, gt=0)

    sandbox_backend: Literal["docker", "subprocess"] = "subprocess"
    sandbox_timeout_s: int = Field(120, gt=0)
    sandbox_memory_mb: int = Field(1024, gt=63)
    sandbox_docker_image: str = "oran-adapt-sandbox:latest"

    live_alias: str = "live"
    # Points at the newest registered, validated candidate (whether or not it went live).
    candidate_alias: str = "candidate"
    log_level: str = "INFO"
    log_json: bool = True

    # Phase 10 hardening: the job wrapper retries a job up to job_max_retries times (exponential
    # backoff starting at job_retry_backoff_s) when the pipeline raises a transient error
    # (MLflow or PostgreSQL unreachable), and gives the whole pipeline run at most job_timeout_s
    # wall-clock seconds before recording the job FAILED with a JOB_TIMEOUT error.
    job_max_retries: int = Field(2, ge=0)
    job_retry_backoff_s: float = Field(1.0, ge=0)
    job_timeout_s: float = Field(600.0, gt=0)

    # Member 1 (analysis) reuse thresholds. A feature is treated as "shifted" once its PSI
    # crosses analysis_psi_reuse_threshold OR its KS test p-value drops below
    # analysis_ks_pvalue_reuse_threshold; a caller-supplied drift_score at or above
    # analysis_drift_score_reuse_threshold forces non-reuse outright.
    analysis_psi_reuse_threshold: float = Field(0.1, ge=0)
    analysis_ks_pvalue_reuse_threshold: float = Field(0.05, gt=0, lt=1)
    analysis_drift_score_reuse_threshold: float = Field(0.3, ge=0, le=1)

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

    # Torch engine budgets (full-batch Adam steps). A from-scratch retrain needs far more steps
    # than a warm-start fine-tune to get back to the current model's quality.
    torch_fine_tune_epochs: int = Field(5, ge=1)
    torch_full_retrain_epochs: int = Field(300, ge=1)
    torch_learning_rate: float = Field(1e-2, gt=0)  # Adam step size for both torch engines

    # API authentication and roles. Callers send ``X-API-Key: <key>`` (or ``Authorization:
    # Bearer <key>``). Keys are never stored: API_KEYS maps the SHA-256 hex digest of each key to
    # "ROLE" or "ROLE:caller-name", e.g. API_KEYS='{"9f86d0...": "OPERATOR:team1"}'
    # (`oran-adapt auth new-key` makes a key and its entry). With auth enabled and no keys
    # configured, every protected endpoint refuses (fail closed). /health, /readiness and, with
    # metrics_public, /metrics need no key.
    auth_enabled: bool = True
    api_keys: dict[str, str] = Field(default_factory=dict)
    metrics_public: bool = True

    @model_validator(mode="after")
    def _llm_key_present(self) -> Settings:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        if self.llm_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
        return self

    @model_validator(mode="after")
    def _api_keys_well_formed(self) -> Settings:
        from oran_adapt.core.enums import Role

        for digest, spec in self.api_keys.items():
            if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
                raise ValueError("API_KEYS keys must be SHA-256 hex digests of the API keys")
            if spec.partition(":")[0] not in Role.__members__:
                raise ValueError(f"API_KEYS role must be one of {', '.join(Role)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
