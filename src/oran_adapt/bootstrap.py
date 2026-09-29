"""Composition root: the one place adapters are chosen.

``build_container(settings)`` resolves every configured port to its adapter (oran_adapt.plugins)
exactly once and returns them together. The API app, the CLI and the CDC worker each call it at
startup; nothing below this layer names an adapter or a vendor. An unknown adapter name, or an
adapter whose required keys are unset, fails here with a ConfigurationError naming the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from oran_adapt import plugins
from oran_adapt.core.config import selected_adapters
from oran_adapt.core.errors import ConfigurationError
from oran_adapt.datastore.access import DataAccess
from oran_adapt.llm.client import InstrumentedLlmClient
from oran_adapt.registry.deployment import Deployer
from oran_adapt.registry.handlers import ModelHandlers

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import (
        ArtifactStorePort,
        AuthPort,
        CdcSourcePort,
        DatasetPort,
        DeploymentPort,
        JobExecutorPort,
        JobQueuePort,
        LLMPort,
        ModelHandlerPort,
        ModelRegistryPort,
        NotificationPort,
        PolicyPort,
        RolloutMetricsPort,
        SecretsPort,
    )

LLM_DISABLED = "none"
CDC_DISABLED = "disabled"


def _make(port: str, settings: Settings, config_key: str) -> Any:
    """The adapter that ``settings.<config_key>`` names for ``port``, built from the settings."""
    spec = plugins.resolve(port, getattr(settings, config_key), config_key=config_key)
    return spec.factory(settings)


@dataclass(frozen=True)
class Container:
    settings: Settings
    registry: ModelRegistryPort
    deployer: Deployer
    model_handler: ModelHandlerPort
    llm: LLMPort | None
    job_executor: JobExecutorPort
    job_queue: JobQueuePort
    auth: AuthPort
    policy: PolicyPort
    notifiers: dict[str, NotificationPort]
    secrets: SecretsPort
    data_access: DataAccess
    rollout_metrics: RolloutMetricsPort


def build_registry(settings: Settings) -> ModelRegistryPort:
    registry: ModelRegistryPort = _make("registry", settings, "registry_backend")
    return registry


def build_deployment(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    """The serving system DEPLOYMENT_BACKEND names. Deployment adapters are built from the
    settings and the registry (the default, registry-alias, serves by moving a registry alias;
    the cloud adapters serve versions of their own registry)."""
    spec = plugins.resolve("deployment", settings.deployment_backend,
                           config_key="deployment_backend")
    deployment = cast("DeploymentPort", spec.factory(settings, registry))
    return deployment


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


def build_deployer(settings: Settings, registry: ModelRegistryPort) -> Deployer:
    """The serving system wrapped with read-back. Fails at startup when DELIVERY_STRATEGY needs
    a traffic split the adapter does not offer."""
    spec = plugins.resolve("deployment", settings.deployment_backend,
                           config_key="deployment_backend")
    deployer = Deployer.from_settings(build_deployment(settings, registry), settings,
                                      spec.capability.features)
    if needs_traffic_split(settings) and not deployer.splits_traffic:
        raise ConfigurationError(
            f"DELIVERY_STRATEGY={settings.delivery_strategy} needs a deployment backend with "
            f"the traffic_split feature; {settings.deployment_backend} has none",
            key="delivery_strategy",
            backend=settings.deployment_backend,
            with_traffic_split=sorted(
                name for name, s in plugins.adapters("deployment").items()
                if "traffic_split" in s.capability.features
            ),
        )
    return deployer


def build_rollout_metrics(settings: Settings) -> RolloutMetricsPort:
    """Where rollouts read online health metrics (ROLLOUT_METRICS_BACKEND)."""
    source: RolloutMetricsPort = _make("rollout_metrics", settings, "rollout_metrics_backend")
    return source


def build_model_handler(settings: Settings) -> ModelHandlerPort:
    """Every installed model handler: loading picks the one that recognises an artifact,
    saving uses the one MODEL_FORMAT names."""
    handlers: dict[str, ModelHandlerPort] = {
        name: cast("ModelHandlerPort", spec.factory(settings))
        for name, spec in plugins.adapters("model_handler").items()
    }
    return ModelHandlers(handlers, settings.model_format)


def build_artifact_store(settings: Settings) -> ArtifactStorePort:
    store: ArtifactStorePort = _make("artifact_store", settings, "artifact_store_backend")
    return store


def build_llm(settings: Settings) -> LLMPort | None:
    """None means "no LLM configured" (LLM_PROVIDER=none): callers keep a deterministic path
    for that case rather than treating it as an error."""
    if settings.llm_provider == LLM_DISABLED:
        return None
    return InstrumentedLlmClient(_make("llm", settings, "llm_provider"), settings.llm_provider)


def build_job_executor(settings: Settings) -> JobExecutorPort:
    executor: JobExecutorPort = _make("job_executor", settings, "job_execution_mode")
    return executor


def build_job_queue(settings: Settings) -> JobQueuePort:
    """How workers are woken for a queued job (JOB_QUEUE_BACKEND; the queue is the database)."""
    queue: JobQueuePort = _make("job_queue", settings, "job_queue_backend")
    return queue


def build_cdc_source(settings: Settings) -> CdcSourcePort:
    """Built on demand by the CDC worker, not at startup: a broker-backed source connects."""
    from oran_adapt.core.errors import CdcUnavailableError

    if settings.cdc_mode == CDC_DISABLED:
        raise CdcUnavailableError(
            "CDC is disabled (set CDC_MODE to an installed cdc_source adapter)",
            available=sorted(plugins.adapters("cdc_source")),
        )
    source: CdcSourcePort = _make("cdc_source", settings, "cdc_mode")
    return source


def build_notifiers(settings: Settings) -> dict[str, NotificationPort]:
    """Every notification sink NOTIFICATION_BACKEND names (none for "none"), by name."""
    return {
        name: cast(
            "NotificationPort",
            plugins.resolve("notification", name, config_key="notification_backend")
            .factory(settings),
        )
        for name in selected_adapters(settings, "notification_backend")
    }


def build_data_access(settings: Settings) -> DataAccess:
    """Row access for every data version, reading referenced objects through each dataset
    adapter DATASET_BACKENDS names (none by default: only database-stored rows)."""
    backends = [
        cast(
            "DatasetPort",
            plugins.resolve("dataset", name, config_key="dataset_backends").factory(settings),
        )
        for name in selected_adapters(settings, "dataset_backends")
    ]
    return DataAccess.from_settings(settings, backends)


def build_auth(settings: Settings) -> AuthPort:
    auth: AuthPort = _make("auth", settings, "auth_backend")
    return auth


def build_policy(settings: Settings) -> PolicyPort:
    policy: PolicyPort = _make("policy", settings, "policy_backend")
    return policy


def build_container(settings: Settings) -> Container:
    registry = build_registry(settings)
    return Container(
        settings=settings,
        registry=registry,
        deployer=build_deployer(settings, registry),
        model_handler=build_model_handler(settings),
        llm=build_llm(settings),
        job_executor=build_job_executor(settings),
        job_queue=build_job_queue(settings),
        auth=build_auth(settings),
        policy=build_policy(settings),
        notifiers=build_notifiers(settings),
        secrets=_make("secrets", settings, "secrets_backend"),
        data_access=build_data_access(settings),
        rollout_metrics=build_rollout_metrics(settings),
    )
