"""Ports: the interfaces the core depends on. Adapters implement them (``oran_adapt.adapters``
and third-party distributions) and are resolved once, by name, at the composition root
(``oran_adapt.bootstrap``) via the ``oran_adapt.<port>`` entry-point groups."""

from oran_adapt.ports.capability import AdapterSpec, Capability
from oran_adapt.ports.registry import (
    ArtifactStorePort,
    ModelHandlerPort,
    ModelRegistryPort,
    ModelVersion,
    RegisteredModel,
)
from oran_adapt.ports.runtime import (
    CdcSourcePort,
    DatasetPort,
    DeploymentPort,
    DeploymentState,
    DeploymentTarget,
    JobCall,
    JobExecutorPort,
    LLMPort,
    NotificationPort,
    OutboundMessage,
    SourceStat,
)
from oran_adapt.ports.security import AuthPort, PolicyPort, Principal, SecretsPort

# Port name -> interface. The name is also the entry-point group suffix and the config key stem.
PORTS: dict[str, type] = {
    "registry": ModelRegistryPort,
    "artifact_store": ArtifactStorePort,
    "model_handler": ModelHandlerPort,
    "deployment": DeploymentPort,
    "dataset": DatasetPort,
    "cdc_source": CdcSourcePort,
    "job_executor": JobExecutorPort,
    "notification": NotificationPort,
    "llm": LLMPort,
    "auth": AuthPort,
    "policy": PolicyPort,
    "secrets": SecretsPort,
}

__all__ = [
    "PORTS",
    "AdapterSpec",
    "ArtifactStorePort",
    "AuthPort",
    "Capability",
    "CdcSourcePort",
    "DatasetPort",
    "DeploymentPort",
    "DeploymentState",
    "DeploymentTarget",
    "JobCall",
    "JobExecutorPort",
    "LLMPort",
    "ModelHandlerPort",
    "ModelRegistryPort",
    "ModelVersion",
    "NotificationPort",
    "OutboundMessage",
    "PolicyPort",
    "Principal",
    "RegisteredModel",
    "SecretsPort",
    "SourceStat",
]
