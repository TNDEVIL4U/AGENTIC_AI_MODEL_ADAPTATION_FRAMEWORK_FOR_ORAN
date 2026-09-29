"""Deployment adapter ``registry-alias`` (the default): the served version is the one a
registry alias points at, and serving processes load ``model://<name>@<alias>``.

With DEPLOYMENT_ALIAS unset the alias is LIVE_ALIAS itself, which is how the framework behaved
before deployment was a port: "deployed" means "live". Setting DEPLOYMENT_ALIAS (e.g.
``serving``) keeps a separate marker that moves only once the rollout has been read back.
An alias move is immediate, so ``status`` is always settled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oran_adapt.core.errors import ConfigurationError, ModelNotFoundError
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class RegistryAliasDeployment:
    def __init__(self, registry: ModelRegistryPort, alias: str) -> None:
        self.registry = registry
        self.alias = alias

    @classmethod
    def from_settings(cls, settings: Settings, registry: ModelRegistryPort) -> RegistryAliasDeployment:
        alias = settings.deployment_alias or settings.live_alias
        if alias == settings.candidate_alias:
            raise ConfigurationError(
                "DEPLOYMENT_ALIAS must differ from CANDIDATE_ALIAS: the candidate alias moves "
                "before validation, the served one only after a promotion",
                key="deployment_alias",
            )
        return cls(registry, alias)

    def ping(self) -> None:
        self.registry.ping()

    def status(self, model: str) -> DeploymentState:
        try:
            version: str | None = self.registry.get_version_by_alias(model, self.alias)
        except ModelNotFoundError:
            version = None
        return DeploymentState(model=model, version=version, ready=True)

    def deploy(self, target: DeploymentTarget) -> None:
        self.registry.set_alias(target.model, self.alias, target.version)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is None:
            self.registry.delete_alias(model, self.alias)
        else:
            self.registry.set_alias(model, self.alias, previous.version)


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return RegistryAliasDeployment.from_settings(settings, registry)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="registry-alias",
        description="serving follows a registry alias (DEPLOYMENT_ALIAS, default LIVE_ALIAS)",
        features=frozenset({"undeploy", "instant"}),
        config_keys=("deployment_alias", "live_alias", "candidate_alias"),
    ),
    factory=_build,
)
