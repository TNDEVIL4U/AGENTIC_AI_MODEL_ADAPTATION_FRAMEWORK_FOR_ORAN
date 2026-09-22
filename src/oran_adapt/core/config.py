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
    gemini_model: str = "gemini-2.5-pro"
    llm_timeout_s: float = Field(60.0, gt=0)

    sandbox_backend: Literal["docker", "subprocess"] = "subprocess"
    sandbox_timeout_s: int = Field(120, gt=0)
    sandbox_memory_mb: int = Field(1024, gt=63)
    sandbox_docker_image: str = "oran-adapt-sandbox:latest"

    live_alias: str = "live"
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
    validation_min_rows: int = Field(5, ge=1)
    validation_accuracy_tolerance: float = Field(0.02, ge=0, le=1)
    validation_rmse_tolerance_ratio: float = Field(0.05, ge=0)

    @model_validator(mode="after")
    def _llm_key_present(self) -> Settings:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        if self.llm_provider == "gemini" and self.gemini_api_key is None:
            raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
