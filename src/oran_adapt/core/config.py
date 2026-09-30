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
from oran_adapt.core.policies import DeliveryPolicy, GatePolicy, load_policy_file

# Adapter selector key -> port. Each selected adapter's Capability.required_keys must be set.
ADAPTER_SELECTORS: dict[str, str] = {
    "registry_backend": "registry",
    "deployment_backend": "deployment",
    "model_format": "model_handler",
    "artifact_store_backend": "artifact_store",
    "llm_provider": "llm",
    "job_execution_mode": "job_executor",
    "job_queue_backend": "job_queue",
    "cdc_mode": "cdc_source",
    "auth_backend": "auth",
    "policy_backend": "policy",
    "notification_backend": "notification",
    "dataset_backends": "dataset",
    "secrets_backend": "secrets",
    "rollout_metrics_backend": "rollout_metrics",
}
# Selector values that mean "this port is switched off" rather than naming an adapter.
DISABLED = {
    "llm_provider": "none",
    "cdc_mode": "disabled",
    "notification_backend": "none",
    "dataset_backends": "none",
}
# Selectors switched on by a separate boolean key: with the switch false the selector selects
# nothing, so its adapter is neither built nor checked for its required keys.
ENABLE_SWITCHES = {"llm_provider": "llm_enabled"}
# Selectors that take a comma-separated list of adapters, all of them in use at once (every
# notification sink named receives every event its filter lets through; every dataset adapter
# named serves the URI schemes it declares).
MULTI_SELECTORS = frozenset({"notification_backend", "dataset_backends"})
# Keys a production deployment must set explicitly (a default would point at a local file).
# Each selected adapter adds its own storage keys (Capability.production_keys).
PRODUCTION_REQUIRED = ("database_url", "artifact_workdir")
# Selectors whose adapter is built only by another adapter: in use when a selected adapter lists
# the selector among its config_keys (the filesystem registry uses ARTIFACT_STORE_BACKEND).
SUBORDINATE_SELECTORS = frozenset({"artifact_store_backend"})

_ALL_ROLES = ["ADMIN", "OPERATOR", "ML_ENGINEER", "READ_ONLY"]


def selected_adapters(settings: Any, selector: str) -> list[str]:
    """The adapter names ``selector`` selects: none when it holds its DISABLED value, every
    comma-separated name for a MULTI_SELECTORS key, otherwise the one name it holds."""
    switch = ENABLE_SWITCHES.get(selector)
    if switch is not None and not getattr(settings, switch):
        return []
    value = str(getattr(settings, selector))
    if DISABLED.get(selector) == value:
        return []
    if selector in MULTI_SELECTORS:
        return list(dict.fromkeys(n.strip() for n in value.split(",") if n.strip()))
    return [value]


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

    # "production" refuses defaulted storage locations (PRODUCTION_REQUIRED and the selected
    # adapters' production_keys).
    environment: Literal["development", "production"] = "development"

    database_url: str = "sqlite:///./data/oran_adapt.db"
    mlflow_tracking_uri: str = "sqlite:///./data/mlflow.db"
    mlflow_registry_uri: str | None = None
    artifact_workdir: str = "./data/artifacts"

    # Model registry adapter (entry point group oran_adapt.registry).
    registry_backend: str = "mlflow"
    # Model handler (oran_adapt.model_handler) that serializes new versions. Loading uses
    # whichever installed handler recognises a version's artifact, whatever this names.
    model_format: str = "mlflow-flavors"
    # Artifact store (oran_adapt.artifact_store) behind the filesystem registry.
    artifact_store_backend: str = "filesystem"
    artifact_store_root: str = "./data/artifact-store"
    # fsspec URL of the object-store adapter, e.g. s3://bucket/prefix (needs s3fs), gs://...
    # (gcsfs), abfs://... (adlfs), or memory://name for tests.
    artifact_store_url: str | None = None
    # Registry adapter "filesystem": version metadata as JSON files under this directory (a
    # local disk or a shared volume), artifacts in the artifact store above.
    registry_fs_root: str = "./data/registry"
    registry_fs_lock_timeout_s: float = Field(30.0, gt=0)
    # Registry adapter "mirror": every read from the primary, every write to the primary and
    # then the replica (both are registry adapter names, and must differ). "fail" raises when
    # the replica write fails; "log" logs it and carries on with the primary's result.
    registry_mirror_primary: str | None = None
    registry_mirror_replica: str | None = None
    registry_mirror_on_replica_error: Literal["fail", "log"] = "fail"
    # Registry adapter "sagemaker": model package groups; artifacts as model.tar.gz in S3.
    # Credentials come from the standard AWS chain, never from this file.
    sagemaker_region: str | None = None
    sagemaker_s3_bucket: str | None = None
    sagemaker_s3_prefix: str = "oran-models"
    sagemaker_inference_image: str | None = None
    sagemaker_group_prefix: str = ""
    # Request / response content types declared in each version's InferenceSpecification.
    sagemaker_content_types: list[str] = ["application/json", "text/csv"]
    # Endpoint override (a VPC endpoint or an emulator); None uses the regional endpoint.
    sagemaker_endpoint_url: str | None = None
    # Registry adapter "vertex": Vertex AI Model Registry over REST; artifacts in GCS.
    # Credentials come from Google Application Default Credentials.
    vertex_project: str | None = None
    vertex_location: str | None = None
    vertex_gcs_bucket: str | None = None
    vertex_gcs_prefix: str = "oran-models"
    vertex_serving_image: str | None = None
    vertex_api_endpoint: str | None = None  # None: https://<location>-aiplatform.googleapis.com
    vertex_storage_endpoint: str = "https://storage.googleapis.com"
    vertex_http_timeout_s: float = Field(30.0, gt=0)
    vertex_operation_timeout_s: float = Field(600.0, gt=0)
    vertex_operation_poll_s: float = Field(5.0, gt=0)
    # Attempts at a conditional (generation-matched) write of a version's tag file when another
    # writer changed it in between.
    vertex_tag_update_attempts: int = Field(5, ge=1)
    # Deployment adapter (oran_adapt.deployment): where a promoted version serves traffic. Every
    # promotion and rollback rolls the version out and reads it back from the serving system,
    # polling every DEPLOYMENT_POLL_S for at most DEPLOYMENT_TIMEOUT_S, and restores the
    # previous version when that fails.
    deployment_backend: str = "registry-alias"
    deployment_timeout_s: float = Field(600.0, gt=0)
    deployment_poll_s: float = Field(5.0, gt=0)
    # Per-request timeout of the HTTP-based deployment adapters.
    deployment_http_timeout_s: float = Field(30.0, gt=0)
    # "registry-alias": the alias that marks the served version; None means LIVE_ALIAS itself
    # (serving reads the live alias, so promotion and deployment are one move).
    deployment_alias: str | None = None
    # "registry-alias" traffic split: the alias that marks the canary/A-B candidate, and the
    # version tag that holds its traffic percentage (serving systems route by both).
    deployment_canary_alias: str = Field("canary", min_length=1)
    deployment_traffic_tag: str = Field("oran.traffic_percent", min_length=1)
    # "webhook": POST <url>/deploy and GET <url>/status (docs/adapters/deployment.md).
    deployment_webhook_url: str | None = None
    deployment_webhook_token: SecretStr | None = None
    # "bentoml": a BentoML service built from templates/bentoml-service (same contract, /oran).
    bentoml_url: str | None = None
    bentoml_token: SecretStr | None = None
    # "gitops": commit a manifest per model to a git checkout (and push); the serving system,
    # synced from git by Argo CD / Flux, is read back through GITOPS_STATUS_URL (webhook status
    # contract). The manifest path takes {model} and {name}.
    gitops_repo_dir: str | None = None
    gitops_manifest_path: str = "deployments/{name}.json"
    # A file whose text is the manifest, with $model, $name, $version and $source substituted;
    # None writes a JSON document with those fields.
    gitops_manifest_template: str | None = None
    gitops_push: bool = False
    gitops_remote: str = "origin"
    gitops_branch: str | None = None  # None: the checkout's current branch
    gitops_status_url: str | None = None
    gitops_status_token: SecretStr | None = None
    gitops_author_name: str = "oran-adapt"
    gitops_author_email: str = "oran-adapt@localhost"
    gitops_git_timeout_s: float = Field(60.0, gt=0)
    # Kubernetes API (adapters "kserve", "seldon", "k8s"). The token is K8S_TOKEN, or read from
    # K8S_TOKEN_FILE on every request (projected service-account tokens rotate); with neither,
    # requests carry no credentials (kubectl proxy).
    k8s_api_url: str | None = None
    k8s_namespace: str = "default"
    k8s_token: SecretStr | None = None
    k8s_token_file: str | None = None
    k8s_ca_file: str | None = None  # None: the system trust store
    # Kubernetes object names are the model name made DNS-1123-safe, after this prefix.
    k8s_name_prefix: str = ""
    # "kserve": an InferenceService per model; storageUri from this template.
    kserve_storage_uri_template: str | None = None
    kserve_model_format: str = "mlflow"
    # "seldon": a Seldon Core v2 Model per model; storageUri from this template.
    seldon_storage_uri_template: str | None = None
    seldon_requirements: list[str] = ["mlflow"]
    # "k8s": an existing Deployment per model; the version is set as an env var of the pod
    # template (K8S_MODEL_ENV = K8S_MODEL_URI_TEMPLATE) and read back from the rollout status.
    k8s_model_env: str = "MODEL_URI"
    k8s_model_uri_template: str = "model://{model}/{version}"
    k8s_container: str | None = None  # None: the pod's only container
    # "triton": NVIDIA Triton (or any server with the KServe v2 repository extension) in
    # explicit model-control mode; artifacts are staged into TRITON_REPOSITORY/<name>/<version>.
    triton_url: str | None = None
    triton_repository: str | None = None
    # A config.pbtxt to start each model's config from (backend, inputs...); the version
    # policy is appended. None leaves the rest to Triton's auto-complete.
    triton_base_config: str | None = None
    # "sagemaker": a real-time endpoint per model, serving model packages of the sagemaker
    # registry (region and endpoint override as above).
    sagemaker_deploy_role_arn: str | None = None
    sagemaker_instance_type: str = "ml.m5.large"
    sagemaker_instance_count: int = Field(1, ge=1)
    sagemaker_endpoint_prefix: str = ""
    # "vertex": a Vertex AI endpoint per model, serving versions of the vertex registry.
    vertex_machine_type: str = "n1-standard-2"
    vertex_min_replicas: int = Field(1, ge=1)
    vertex_max_replicas: int = Field(1, ge=1)
    vertex_endpoint_prefix: str = ""
    # MLflow client HTTP behaviour, applied to MLFLOW_HTTP_REQUEST_* unless those are set.
    mlflow_http_max_retries: int = Field(1, ge=0)
    mlflow_http_backoff_factor: float = Field(0.0, ge=0)
    mlflow_http_timeout_s: float = Field(10.0, gt=0)
    # MLflow's usage telemetry to its vendor (off: no call leaves the deployment unasked).
    mlflow_telemetry: bool = False
    # Registry tag names the framework writes on model versions.
    registry_tags_checksum: str = Field("artifact.sha256", min_length=1)
    registry_tags_status: str = Field("oran.status", min_length=1)

    # The LLM is off unless LLM_ENABLED is true: every decision and adaptation then takes the
    # deterministic path and nothing is sent anywhere. LLM_PROVIDER names the adapter
    # (oran_adapt.llm group) used when it is on.
    llm_enabled: bool = False
    llm_provider: str = "none"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    # Messages API base URL; unset means the SDK default (api.anthropic.com).
    anthropic_base_url: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    # generate_content base URL; unset means the SDK default (generativelanguage.googleapis.com).
    gemini_base_url: str | None = None
    llm_timeout_s: float = Field(60.0, gt=0)
    llm_max_output_tokens: int = Field(2048, ge=1)
    # Retries after the first attempt, made by the guard (llm.guard) with exponential backoff
    # from LLM_RETRY_BACKOFF_S; provider SDKs never retry on their own.
    llm_max_retries: int = Field(0, ge=0)
    llm_retry_backoff_s: float = Field(0.5, ge=0)
    # Circuit breaker: this many failed calls in a row open it; after LLM_BREAKER_RESET_S one
    # trial call is let through.
    llm_breaker_failure_threshold: int = Field(3, ge=1)
    llm_breaker_reset_s: float = Field(60.0, gt=0)
    # Caps. A call whose prompt exceeds LLM_MAX_INPUT_TOKENS, or that could take the budget
    # window past LLM_TOKEN_BUDGET or LLM_COST_BUDGET (counting LLM_MAX_OUTPUT_TOKENS as spent),
    # is refused before it is sent. 0 means no cap. Usage is kept in the llm_usage table.
    llm_max_input_tokens: int = Field(16000, ge=0)
    llm_token_budget: int = Field(0, ge=0)
    llm_cost_budget: float = Field(0.0, ge=0)
    llm_budget_window_s: int = Field(86400, gt=0)
    # Prices per 1000 tokens, in the currency LLM_COST_BUDGET is in (0: cost not tracked).
    llm_cost_per_1k_input_tokens: float = Field(0.0, ge=0)
    llm_cost_per_1k_output_tokens: float = Field(0.0, ge=0)
    # Token estimate for providers that do not report usage, and for the pre-call check.
    llm_chars_per_token: float = Field(4.0, gt=0)
    # Prompt version per prompt id (default: the newest built-in version); LLM_PROMPT_DIR adds
    # versions from files named <id>@<version>.txt.
    llm_prompt_versions: dict[str, str] = Field(default_factory=dict)
    llm_prompt_dir: str | None = None
    # OpenAI-compatible chat completions endpoint (vLLM, Ollama, LM Studio, LiteLLM, Azure
    # OpenAI behind a gateway, ...): LLM_PROVIDER=openai-compatible.
    llm_openai_base_url: str | None = None
    llm_openai_model: str | None = None
    llm_openai_api_key: SecretStr | None = None

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

    # Tracing (core.tracing, docs/operations/observability.md): one trace per drift event, from
    # intake to deployment verification. TRACING_EXPORTER: "none" (default; the OpenTelemetry
    # API stays a no-op), "console" (spans as JSON on stderr), "jsonl" (one JSON line per span
    # appended to TRACING_JSONL_PATH, safe across worker processes) or "otlp" (OTLP/HTTP to
    # TRACING_OTLP_ENDPOINT; needs the opentelemetry-exporter-otlp-proto-http package).
    tracing_exporter: Literal["none", "console", "jsonl", "otlp"] = "none"
    tracing_service_name: str = "oran-adapt"
    tracing_sample_ratio: float = Field(1.0, ge=0, le=1)
    tracing_jsonl_path: str | None = None
    tracing_otlp_endpoint: str | None = None
    tracing_otlp_timeout_s: float = Field(10.0, gt=0)
    # A worker serves its own /metrics on this port (unset: none). The API serves /api/v1/metrics;
    # a worker process is a separate pod, so without this its job metrics are never scraped.
    worker_metrics_port: int | None = Field(None, ge=1, le=65535)
    # The address it binds; empty means every interface (a pod's own port).
    worker_metrics_addr: str = ""

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

    # The job queue (orchestrator.worker, docs/adapters/job_queue.md). The API only records a job
    # QUEUED; workers (`oran-adapt worker run`) claim and run it. The database holds the queue,
    # so it survives any broker; JOB_QUEUE_BACKEND only says how workers are woken:
    # "database" (workers poll), "celery", "rq", "kubernetes" (a Job per attempt), or "inline"
    # (the submitting call runs the job itself: tests and development only).
    job_queue_backend: str = "database"
    # The checkpoint interval: how often a running job's supervisor renews its lease and model
    # lock and looks for a cancel request, the deadline and a drain.
    job_heartbeat_s: float = Field(5.0, gt=0)
    # A lease not renewed for this long is lost: the reaper requeues the job (or quarantines it).
    job_lease_ttl_s: float = Field(30.0, gt=0)
    # How often an idle worker looks for a job; how often workers run the reaper.
    job_poll_interval_s: float = Field(1.0, gt=0)
    job_reap_interval_s: float = Field(10.0, gt=0)
    # A queued job the broker has not delivered for this long is published again.
    job_republish_after_s: float = Field(300.0, gt=0)
    # Attempts that may end without an outcome (worker killed, lease lost) before the job is
    # quarantined (FAILED, JOB_QUARANTINED) instead of being run again.
    job_poison_threshold: int = Field(3, ge=1)
    # How long a stopping worker (SIGTERM, SIGINT, Ctrl+Break) lets its running job continue
    # before stopping it and putting it back in the queue.
    job_drain_timeout_s: float = Field(30.0, ge=0)
    # Wall-clock limit of a job from submission, queue time included (unset: only the
    # per-attempt JOB_TIMEOUT_S applies).
    job_deadline_s: float | None = Field(None, gt=0)
    # Worker classes: a job runs only on workers serving its class. The class comes from the
    # model's framework through JOB_CLASS_BY_FRAMEWORK ({"torch": "gpu"}), else
    # JOB_DEFAULT_CLASS. JOB_WORKER_CLASSES: the classes a worker serves unless --classes says.
    job_default_class: str = "default"
    job_class_by_framework: dict[str, str] = {}
    job_worker_classes: list[str] = ["default"]
    # Liveness file: the worker touches it on every loop turn and every job checkpoint, and
    # `oran-adapt worker health` (the image HEALTHCHECK, a Kubernetes exec probe) fails when it
    # is older than WORKER_HEALTH_MAX_AGE_S. Unset: no file is written.
    worker_health_file: str | None = None
    worker_health_max_age_s: float = Field(120.0, gt=0)
    # `oran-adapt db wait` (the init container before the API and the workers): how long to
    # wait for the migration job to bring the schema up to this release, and how often to look.
    migration_wait_timeout_s: float = Field(600.0, gt=0)
    migration_wait_interval_s: float = Field(2.0, gt=0)
    # Claim order: higher first, then oldest first. Taken from the event's severity.
    job_priority_by_severity: dict[str, int] = {"CRITICAL": 30, "HIGH": 20, "MEDIUM": 10, "LOW": 0}
    job_default_priority: int = 0
    # How many of the next claimable jobs a worker tries per poll: a candidate another worker
    # wins, or whose tenant is at its limit, is skipped for the next one.
    job_claim_candidates: int = Field(10, ge=1)
    # Tenants: the submitting principal's name, mapped through JOB_TENANT_BY_PRINCIPAL. At most
    # JOB_TENANT_LIMITS[tenant] (else JOB_TENANT_CONCURRENCY; 0 = unlimited) of a tenant's jobs
    # run at once.
    job_tenant_by_principal: dict[str, str] = {}
    job_default_tenant: str = "default"
    job_tenant_concurrency: int = Field(0, ge=0)
    job_tenant_limits: dict[str, int] = {}
    # "celery": the task is published to queue <JOB_QUEUE_NAME_PREFIX><class>.
    job_queue_name_prefix: str = "oran-jobs-"
    job_queue_celery_broker_url: SecretStr | None = None
    job_queue_celery_task: str = "oran_adapt.run_job"
    # "rq": a Redis Queue per class, running oran_adapt.orchestrator.worker.run_job_by_id.
    job_queue_rq_redis_url: SecretStr | None = None
    # "kubernetes": a batch/v1 Job per attempt, through the K8S_* API settings. The pod runs
    # `oran-adapt worker run-job --job-id <id>` in JOB_QUEUE_K8S_IMAGE; JOB_QUEUE_K8S_CLASS_PODS
    # adds pod spec fields per class ({"gpu": {"nodeSelector": {...}, "resources": {...}}}).
    job_queue_k8s_image: str | None = None
    job_queue_k8s_env_secret: str | None = None
    job_queue_k8s_service_account: str | None = None
    job_queue_k8s_class_pods: dict[str, dict[str, Any]] = {}
    job_queue_k8s_ttl_after_finished_s: int = Field(3600, ge=0)

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
    # decision_supported_frameworks has no adaptation engine to carry a decision out (empty, the
    # default: every framework an installed model type plugin adapts); a max_psi at or above
    # decision_full_retrain_psi_threshold rules out incremental fine-tuning.
    decision_min_drifted_rows: int = Field(10, ge=1)
    decision_full_retrain_psi_threshold: float = Field(0.5, ge=0)
    decision_supported_frameworks: list[str] = Field(default_factory=list)
    # How many copies of a past decision the decision memory keeps per model and strategy.
    decision_memory_copies: int = Field(3, ge=1)
    # Confidence reported for a rule-based decision that carries no score of its own.
    decision_default_confidence: float = Field(0.5, ge=0, le=1)

    # Member 4 (validation). A candidate needs at least validation_min_rows rows of held-out
    # data to be scored at all. The hold-out is the newest validation_holdout_fraction of the
    # drifted rows (at least validation_min_rows of them, always leaving one drifted row to train
    # on). Those rows are never trained on, so both models are scored on data neither has seen.
    validation_min_rows: int = Field(5, ge=1)
    validation_holdout_fraction: float = Field(0.2, gt=0, lt=1)
    # The gate (validation.gate): a statistical test against the incumbent plus guardrails,
    # all thresholds in this versioned policy (core.policies.GatePolicy). GATE_POLICY_FILE, when
    # set, replaces GATE_POLICY with the TOML/JSON file's contents (config/policies/*).
    gate_policy: GatePolicy = Field(default_factory=GatePolicy)
    gate_policy_file: str | None = None
    # Progressive delivery (delivery.controller): how a validated candidate reaches traffic.
    # shadow | canary | blue_green | ab | manual. canary and ab (and shadow/manual handing over
    # to canary) need a deployment adapter with the "traffic_split" feature; startup refuses the
    # combination otherwise. blue_green switches all traffic at once with read-back.
    delivery_strategy: Literal["shadow", "canary", "blue_green", "ab", "manual"] = "shadow"
    delivery_policy: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    delivery_policy_file: str | None = None
    # Monitoring -> DriftEvent mappers (core.event_mapping): mapper name -> mapping file
    # (config/mappers/*.toml). Each is served at POST /api/v1/adaptation/events/from/{name};
    # every file is validated at startup. A payload may hold at most DRIFT_MAPPER_MAX_EVENTS.
    drift_mappers: dict[str, str] = Field(default_factory=dict)
    drift_mapper_max_events: int = Field(100, ge=1)
    # Where the rollout controller reads each arm's online metrics (oran_adapt.rollout_metrics):
    # "api" (observations POSTed to /api/v1/rollouts/{id}/observations) or "prometheus".
    rollout_metrics_backend: str = "api"
    # How often a worker advances active rollouts (also: oran-adapt rollout tick).
    rollout_tick_s: float = Field(30.0, gt=0)
    # "prometheus": PromQL range queries per metric name. Templates may use {model}, {version},
    # {arm} (stable|candidate) and {rollout_id}; each series' samples over the rollout window
    # become that metric's samples. The requests query (same placeholders plus {window_s})
    # counts an arm's observations.
    rollout_prometheus_url: str | None = None
    rollout_prometheus_token: SecretStr | None = None
    rollout_prometheus_queries: dict[str, str] = Field(default_factory=dict)
    rollout_prometheus_requests_query: str | None = None
    rollout_prometheus_step_s: float = Field(60.0, gt=0)
    rollout_prometheus_timeout_s: float = Field(10.0, gt=0)

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
    # The pip requirements written into every saved model's environment. Empty: MLflow infers
    # them, which imports the model in a subprocess (seconds to tens of seconds per save).
    mlflow_pip_requirements: list[str] = Field(default_factory=list)

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

    # Model type plugins (docs/adapters/model_type.md): which installed plugins handle models, in
    # the order they are asked (the first that accepts a model handles it). Empty: every
    # installed plugin, by name.
    model_types: list[str] = Field(default_factory=list)
    # Sequence models (torch-sequence, keras): the rows of history one prediction reads when the
    # model does not declare its own window, the newest share of the training rows held back
    # (in time order) to pick the best epoch, the epoch budgets and the Adam step size.
    sequence_window: int = Field(8, ge=1)
    sequence_validation_fraction: float = Field(0.2, ge=0, lt=1)
    sequence_fine_tune_epochs: int = Field(20, ge=1)
    sequence_full_retrain_epochs: int = Field(200, ge=1)
    sequence_learning_rate: float = Field(1e-2, gt=0)
    # Where ONNX graphs run: "onnxruntime" or the onnx package's "reference" evaluator.
    onnx_runtime: Literal["onnxruntime", "reference"] = "onnxruntime"

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
    # oidc / gateway (JWT) auth: roles come from the AUTH_ROLE_CLAIM claim (a dotted path)
    # mapped through AUTH_ROLE_MAP (claim value -> role); the caller's name from
    # AUTH_NAME_CLAIM. Signature algorithms are an allowlist (asymmetric only).
    auth_oidc_issuer: str | None = None
    auth_oidc_audience: str | None = None
    auth_oidc_jwks_url: str | None = None  # None: from the issuer's discovery document
    auth_gateway_issuers: dict[str, str] = Field(default_factory=dict)  # issuer -> JWKS URL
    auth_gateway_header: str = Field("X-Jwt-Assertion", min_length=1)
    auth_gateway_audience: str | None = None
    auth_jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"],
                                           min_length=1)
    auth_jwt_leeway_s: float = Field(60.0, ge=0)
    auth_role_claim: str = Field("roles", min_length=1)
    auth_role_map: dict[str, str] = Field(default_factory=dict)
    auth_name_claim: str = Field("sub", min_length=1)
    auth_http_timeout_s: float = Field(5.0, gt=0)
    auth_jwks_cache_ttl_s: float = Field(300.0, gt=0)
    auth_jwks_min_refetch_s: float = Field(30.0, ge=0)
    # mtls: a TLS-terminating proxy forwards the client certificate in AUTH_MTLS_CERT_HEADER;
    # it must be issued by AUTH_MTLS_CA_FILE and its identity (CN or SAN) listed in
    # AUTH_MTLS_IDENTITIES (identity -> role).
    auth_mtls_ca_file: str | None = None
    auth_mtls_cert_header: str = Field("X-Forwarded-Client-Cert", min_length=1)
    auth_mtls_identities: dict[str, str] = Field(default_factory=dict)
    # Peers (CIDRs) whose forwarded identity headers (gateway assertion, client certificate)
    # are believed; from anyone else those headers are refused.
    auth_trusted_proxies: list[str] = Field(default_factory=list)
    # Per-replica token-bucket limits (0 turns a limit off): requests per authenticated caller,
    # and failed authentications per client address; refused requests get 429 + Retry-After.
    api_rate_limit_per_minute: int = Field(600, ge=0)
    api_rate_limit_burst: int = Field(100, ge=1)
    api_auth_failure_limit_per_minute: int = Field(30, ge=0)
    api_rate_limit_max_keys: int = Field(10_000, ge=1)
    # Serve /docs, /redoc and /openapi.json; None: on except in production.
    api_docs_enabled: bool | None = None
    # Strict-Transport-Security max-age on every response (0: header not sent; set it when the
    # API is only reachable over HTTPS).
    api_hsts_max_age_s: int = Field(0, ge=0)
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
    # "opa": Open Policy Agent decides (adapters.opa). Each question is a POST to
    # POLICY_OPA_URL/v1/data/POLICY_OPA_PATH with input {action, role, principal}; only a result
    # of true allows. Role answers are cached POLICY_OPA_CACHE_S; any failure refuses.
    policy_opa_url: str | None = None
    policy_opa_path: str = "oran_adapt/authz/allow"
    policy_opa_token: SecretStr | None = None
    policy_opa_timeout_s: float = Field(2.0, gt=0)
    policy_opa_cache_s: float = Field(30.0, ge=0)

    # Outbound notifications (oran_adapt.notifications, docs/adapters/notification.md). Every
    # job state transition is written to a durable outbox in the transaction that makes it; a
    # dispatcher delivers it to each sink NOTIFICATION_BACKEND names (comma-separated, or
    # "none"), at least once, retrying with exponential backoff and dead-lettering after
    # notification_max_attempts. GET /api/v1/deliveries lists deliveries; redrive re-queues.
    notification_backend: str = Field("log", min_length=1)
    # Event types each sink receives (sink -> short types such as "job.failed"); a sink not
    # listed receives every event.
    notification_sink_events: dict[str, list[str]] = Field(
        default_factory=lambda: {"pagerduty": ["job.failed", "job.timed_out", "job.rolled_back"]}
    )
    notification_source: str = Field("oran-adapt", min_length=1)  # CloudEvents "source"
    notification_event_type_prefix: str = "oran.adapt."  # CloudEvents "type" = prefix + type
    # Run the dispatcher inside the API process; off when a separate
    # `oran-adapt notifications dispatch` process delivers instead.
    notification_dispatch_enabled: bool = True
    notification_dispatch_interval_s: float = Field(1.0, gt=0)  # idle wait between polls
    notification_dispatch_batch: int = Field(50, ge=1)  # deliveries claimed per poll
    # A claimed delivery becomes claimable again once its lease runs out (its dispatcher died
    # mid-send); keep it longer than any sink call can take.
    notification_lease_s: float = Field(60.0, gt=0)
    notification_max_attempts: int = Field(8, ge=1)  # then the delivery is dead-lettered
    notification_backoff_initial_s: float = Field(2.0, gt=0)
    notification_backoff_max_s: float = Field(600.0, gt=0)
    notification_backoff_jitter: float = Field(0.2, ge=0, le=1)  # +/- this fraction
    # Consecutive failures that open a sink's circuit; while open its deliveries wait
    # notification_breaker_reset_s, then one trial delivery decides whether it closes.
    notification_breaker_failures: int = Field(5, ge=1)
    notification_breaker_reset_s: float = Field(60.0, gt=0)
    notification_timeout_s: float = Field(10.0, gt=0)  # per sink call
    # HMAC-SHA256 signing keys (Standard Webhooks), comma-separated "whsec_<base64>" or raw
    # secrets. Every key signs each message, so rotating is: add the new key, let receivers
    # accept it, remove the old one. Required in production for the webhook sink.
    notification_signing_keys: SecretStr | None = None
    notification_signing_min_key_bytes: int = Field(32, ge=16)
    # webhook: a signed CloudEvents JSON POST.
    notification_webhook_url: str | None = None
    # slack: an incoming-webhook URL (the URL is the credential).
    notification_slack_webhook_url: SecretStr | None = None
    # pagerduty: Events API v2.
    notification_pagerduty_routing_key: SecretStr | None = None
    notification_pagerduty_url: str = Field(
        "https://events.pagerduty.com/v2/enqueue", min_length=1
    )
    notification_pagerduty_severity: Literal["critical", "error", "warning", "info"] = "error"
    # email: SMTP submission.
    notification_smtp_host: str | None = None
    notification_smtp_port: int = Field(587, ge=1, le=65535)
    notification_smtp_starttls: bool = True
    notification_smtp_username: str | None = None
    notification_smtp_password: SecretStr | None = None
    notification_email_from: str | None = None
    notification_email_to: list[str] = Field(default_factory=list)
    # kafka: one record per event, keyed by the job id (brokers: KAFKA_BOOTSTRAP_SERVERS).
    notification_kafka_topic: str | None = None
    # sqs / sns (region and endpoint shared; credentials from the AWS default chain).
    notification_sqs_queue_url: str | None = None
    notification_sns_topic_arn: str | None = None
    notification_aws_region: str | None = None
    notification_aws_endpoint_url: str | None = None
    # pubsub: Google Pub/Sub REST publish; credentials "adc" (application default) or "none"
    # (an emulator).
    notification_pubsub_project: str | None = None
    notification_pubsub_topic: str | None = None
    notification_pubsub_endpoint: str = Field("https://pubsub.googleapis.com", min_length=1)
    notification_pubsub_credentials: Literal["adc", "none"] = "adc"
    # nats: core NATS publish; "{event_type}" in the subject is replaced by the event's type.
    notification_nats_url: str | None = None  # nats://host:4222 or tls://host:4222
    notification_nats_subject: str = Field("oran.adapt.{event_type}", min_length=1)
    notification_nats_token: SecretStr | None = None

    # Where secret-typed keys (API keys of providers) come from besides the environment
    # (oran_adapt.secrets): "env", "file" (one file per secret in secrets_dir) or "vault"
    # (HashiCorp Vault / OpenBao KV v2: one document at <mount>/data/<path>, the token read
    # from secrets_vault_token_file).
    secrets_backend: str = "env"
    secrets_dir: str | None = None
    secrets_vault_url: str | None = None
    secrets_vault_mount: str = Field("secret", min_length=1)
    secrets_vault_path: str = Field("oran-adapt", min_length=1)
    secrets_vault_token_file: str | None = None
    secrets_vault_namespace: str | None = None
    secrets_vault_timeout_s: float = Field(10.0, gt=0)

    # Outbound HTTP policy (core/outbound.py, docs/security.md), applied to every HTTP client
    # the framework builds. Destinations that are not public addresses are refused unless
    # trusted: OUTBOUND_ALLOWLIST entries (host, ".suffix" or CIDR) and the hosts of every
    # configured endpoint (*_url, *_uri, *_endpoint settings, DATASET_HTTP_ALLOWED_HOSTS).
    outbound_allowlist: list[str] = Field(default_factory=list)
    # Refused even when name resolution is off: loopback names and cloud metadata services.
    outbound_blocked_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "metadata.google.internal", "metadata",
                                 "instance-data"]
    )
    # Resolve host names and refuse any that resolve to a non-public address.
    outbound_resolve_hosts: bool = True
    # Refuse plain http (except to configured http:// endpoints); None: on in production.
    outbound_require_https: bool | None = None
    outbound_tls_min_version: Literal["TLSv1.2", "TLSv1.3"] = "TLSv1.2"
    # A private CA bundle for every outbound client (per-adapter CA files still apply).
    outbound_ca_file: str | None = None

    # Data by reference (docs/adapters/dataset.md). A data version is either rows stored in the
    # database (sent inline) or a URI to a Parquet/CSV/JSONL object that stays where it is and is
    # read in DATASET_CHUNK_ROWS batches through the dataset adapter serving its scheme.
    # DATASET_BACKENDS lists the enabled adapters ("none": inline data only).
    dataset_backends: str = Field("none", min_length=1)
    dataset_chunk_rows: int = Field(10_000, ge=1)
    # Memory ceilings. A job or a frame load refuses (DATA_TOO_LARGE) before reading more than
    # DATASET_MAX_ROWS rows; analysis compares drift on a deterministic sample of at most
    # DATASET_ANALYSIS_MAX_ROWS rows per version; a download is refused past
    # DATASET_MAX_SOURCE_BYTES.
    dataset_max_rows: int = Field(1_000_000, ge=1)
    dataset_analysis_max_rows: int = Field(100_000, ge=1)
    dataset_max_source_bytes: int = Field(4 * 1024**3, ge=1)
    # Default name of the timestamp column in referenced data (per version: timestamp_column).
    dataset_time_column: str = Field("observed_at", min_length=1)
    # Before reading a referenced version: "fingerprint" (compare ETag/generation/mtime, and the
    # content hash when the store has no fingerprint), "hash" (always re-hash while reading), or
    # "off".
    dataset_verify_on_read: Literal["fingerprint", "hash", "off"] = "fingerprint"
    # Where downloads of remote objects are spooled (default: the system temp directory).
    dataset_spool_dir: str | None = None
    # file: local directories references may point into (anything else is refused).
    dataset_file_roots: list[str] = Field(default_factory=list)
    # http: hosts (host or host:port) references may name; https only unless
    # DATASET_HTTP_ALLOW_PLAIN is set.
    dataset_http_allowed_hosts: list[str] = Field(default_factory=list)
    dataset_http_allow_plain: bool = False
    dataset_http_timeout_s: float = Field(30.0, gt=0)
    dataset_http_token: SecretStr | None = None  # sent as "Authorization: Bearer <token>"
    # fsspec: URL prefixes references may start with (s3://bucket/, abfs://container/, ...) and
    # the filesystem storage options as a JSON object (credentials, endpoint).
    dataset_fsspec_prefixes: list[str] = Field(default_factory=list)
    dataset_fsspec_options: SecretStr | None = None
    # s3 (boto3): buckets references may name; region and optional endpoint (MinIO, LocalStack).
    dataset_s3_buckets: list[str] = Field(default_factory=list)
    dataset_s3_region: str | None = None
    dataset_s3_endpoint_url: str | None = None
    # gcs (JSON API over HTTPS): buckets references may name.
    dataset_gcs_buckets: list[str] = Field(default_factory=list)
    dataset_gcs_endpoint: str = Field("https://storage.googleapis.com", min_length=1)
    dataset_gcs_credentials: Literal["adc", "none"] = "adc"

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
    # The source table's row image: primary key, dataset id, timestamp and payload columns.
    # CDC_PAYLOAD_COLUMN="" takes every other column as the payload (a plain wide table);
    # CDC_DATASET_COLUMN="" puts every row into the one dataset CDC_DATASET_ID names.
    cdc_key_column: str = Field("id", min_length=1)
    cdc_dataset_column: str = "dataset_id"
    cdc_dataset_id: str | None = None
    cdc_time_column: str = Field("observed_at", min_length=1)
    cdc_payload_column: str = "payload"

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
    def _data_limits_consistent(self) -> Settings:
        if self.dataset_chunk_rows > self.dataset_max_rows:
            raise ConfigurationError(
                "DATASET_CHUNK_ROWS must not exceed DATASET_MAX_ROWS", key="DATASET_CHUNK_ROWS"
            )
        if not self.cdc_dataset_column and not self.cdc_dataset_id:
            raise ConfigurationError(
                "CDC_DATASET_COLUMN is empty, so CDC_DATASET_ID must name the dataset",
                key="CDC_DATASET_ID",
            )
        return self

    @model_validator(mode="after")
    def _signing_keys_valid(self) -> Settings:
        if self.notification_signing_keys is not None:
            from oran_adapt.notifications.signing import parse_keys

            parse_keys(self.notification_signing_keys.get_secret_value(),
                       self.notification_signing_min_key_bytes)
        return self

    @model_validator(mode="after")
    def _policy_files_loaded(self) -> Settings:
        if self.gate_policy_file:
            self.gate_policy = load_policy_file(self.gate_policy_file, GatePolicy,
                                                "gate_policy_file")
        if self.delivery_policy_file:
            self.delivery_policy = load_policy_file(self.delivery_policy_file, DeliveryPolicy,
                                                    "delivery_policy_file")
        aliases = {self.live_alias, self.candidate_alias, self.deployment_alias}
        if self.deployment_canary_alias in aliases:
            raise ConfigurationError(
                "DEPLOYMENT_CANARY_ALIAS must differ from LIVE_ALIAS, CANDIDATE_ALIAS and "
                "DEPLOYMENT_ALIAS", key="deployment_canary_alias",
            )
        return self

    @model_validator(mode="after")
    def _drift_mappers_valid(self) -> Settings:
        if self.drift_mappers:
            from oran_adapt.core.event_mapping import check_mappers

            check_mappers(self.drift_mappers)
        return self

    @model_validator(mode="after")
    def _llm_switch_consistent(self) -> Settings:
        if self.llm_enabled and self.llm_provider == DISABLED["llm_provider"]:
            raise ConfigurationError(
                "LLM_ENABLED=true requires LLM_PROVIDER to name an LLM adapter",
                key="LLM_PROVIDER",
            )
        return self

    @model_validator(mode="after")
    def _job_timings_consistent(self) -> Settings:
        if self.job_lease_ttl_s <= 2 * self.job_heartbeat_s:
            raise ConfigurationError(
                "JOB_LEASE_TTL_S must be more than twice JOB_HEARTBEAT_S, or one late heartbeat "
                "loses a healthy job's lease",
                key="JOB_LEASE_TTL_S",
                job_lease_ttl_s=self.job_lease_ttl_s,
                job_heartbeat_s=self.job_heartbeat_s,
            )
        slowest_beat = max(self.job_heartbeat_s, self.job_poll_interval_s)
        if self.worker_health_max_age_s <= 2 * slowest_beat:
            raise ConfigurationError(
                "WORKER_HEALTH_MAX_AGE_S must be more than twice the slower of JOB_HEARTBEAT_S "
                "and JOB_POLL_INTERVAL_S, or a healthy worker fails its liveness probe",
                key="WORKER_HEALTH_MAX_AGE_S",
                worker_health_max_age_s=self.worker_health_max_age_s,
                slowest_beat_s=slowest_beat,
            )
        return self

    @model_validator(mode="after")
    def _tracing_complete(self) -> Settings:
        needed = {"jsonl": "tracing_jsonl_path", "otlp": "tracing_otlp_endpoint"}
        key = needed.get(self.tracing_exporter)
        if key is not None and not getattr(self, key):
            raise ConfigurationError(
                f"TRACING_EXPORTER={self.tracing_exporter} requires {key.upper()}",
                key=key.upper(),
            )
        return self

    @model_validator(mode="after")
    def _production_explicit(self) -> Settings:
        if self.environment == "production":
            required = [*PRODUCTION_REQUIRED, *self._adapter_production_keys()]
            fields = type(self).model_fields
            missing = [
                k
                for k in dict.fromkeys(required)
                if k not in self.model_fields_set
                and not (self._secrets_external and is_secret_field(fields[k]))
            ]
            from oran_adapt import plugins

            for selector, port in ADAPTER_SELECTORS.items():
                for name in selected_adapters(self, selector):
                    spec = plugins.resolve(port, name, config_key=selector)
                    if "development_only" in spec.capability.features:
                        raise ConfigurationError(
                            f"{selector.upper()}={name} is for tests and development only, "
                            "not ENVIRONMENT=production",
                            key=selector.upper(),
                        )
            if missing:
                raise ConfigurationError(
                    "ENVIRONMENT=production requires "
                    + ", ".join(k.upper() for k in missing)
                    + " to be set explicitly",
                    key=missing[0].upper(),
                    missing=[k.upper() for k in missing],
                )
        return self

    def _adapter_production_keys(self) -> list[str]:
        """The production_keys of every adapter in use, in selector order."""
        from oran_adapt import plugins

        specs = [
            (selector, plugins.resolve(port, name, config_key=selector))
            for selector, port in ADAPTER_SELECTORS.items()
            for name in selected_adapters(self, selector)
        ]
        referenced = {
            key
            for selector, spec in specs
            if selector not in SUBORDINATE_SELECTORS
            for key in spec.capability.config_keys
        }
        return [
            key
            for selector, spec in specs
            if selector not in SUBORDINATE_SELECTORS or selector in referenced
            for key in spec.capability.production_keys
        ]

    @model_validator(mode="after")
    def _adapter_keys_present(self) -> Settings:
        """Every selected adapter exists and has its required keys set."""
        from oran_adapt import plugins

        selected = [
            (selector, port, name)
            for selector, port in ADAPTER_SELECTORS.items()
            for name in selected_adapters(self, selector)
        ]
        for selector, port, name in selected:
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

    @model_validator(mode="after")
    def _delivery_fits_deployment(self) -> Settings:
        """A delivery that puts a candidate on part of the traffic needs a deployment adapter
        with the traffic_split feature; checked here so config lint catches it too."""
        from oran_adapt import plugins

        if not needs_traffic_split(self):
            return self
        spec = plugins.resolve("deployment", self.deployment_backend,
                               config_key="deployment_backend")
        if "traffic_split" not in spec.capability.features:
            raise ConfigurationError(
                f"DELIVERY_STRATEGY={self.delivery_strategy} needs a deployment backend with "
                f"the traffic_split feature; {self.deployment_backend} has none",
                key="DELIVERY_STRATEGY",
                backend=self.deployment_backend,
                with_traffic_split=sorted(
                    name for name, s in plugins.adapters("deployment").items()
                    if "traffic_split" in s.capability.features
                ),
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


def needs_traffic_split(settings: Settings) -> bool:
    """Whether the configured delivery can put a candidate on part of the traffic (canary, A/B,
    or a shadow / approval that continues as a canary)."""
    policy = settings.delivery_policy
    strategy = settings.delivery_strategy
    return (
        strategy in ("canary", "ab")
        or (strategy == "shadow" and policy.shadow_then == "canary")
        or (strategy in ("manual", "shadow") and policy.approval_then == "canary")
    )


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
