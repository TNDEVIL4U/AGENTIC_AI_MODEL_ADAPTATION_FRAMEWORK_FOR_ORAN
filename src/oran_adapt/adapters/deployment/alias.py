"""Deployment adapter ``registry-alias`` (the default): the served version is the one a
registry alias points at, and serving processes load ``model://<name>@<alias>``.

With DEPLOYMENT_ALIAS unset the alias is LIVE_ALIAS itself, which is how the framework behaved
before deployment was a port: "deployed" means "live". Setting DEPLOYMENT_ALIAS (e.g.
``serving``) keeps a separate marker that moves only once the rollout has been read back.
An alias move is immediate, so ``status`` is always settled.

Traffic split (canary and A/B rollouts): the candidate sits behind DEPLOYMENT_CANARY_ALIAS and
its share of traffic is the version tag DEPLOYMENT_TRAFFIC_TAG. Serving processes that honour
the split send that share of requests to ``model://<name>@<canary alias>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oran_adapt.core.errors import ConfigurationError, ModelNotFoundError
from oran_adapt.ports import (
    AdapterSpec,
    Capability,
    DeploymentState,
    DeploymentTarget,
    TrafficSplit,
)

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class RegistryAliasDeployment:
    def __init__(self, registry: ModelRegistryPort, alias: str, *, canary_alias: str,
                 traffic_tag: str) -> None:
        self.registry = registry
        self.alias = alias
        self.canary_alias = canary_alias
        self.traffic_tag = traffic_tag

    @classmethod
    def from_settings(cls, settings: Settings, registry: ModelRegistryPort) -> RegistryAliasDeployment:
        alias = settings.deployment_alias or settings.live_alias
        if alias == settings.candidate_alias:
            raise ConfigurationError(
                "DEPLOYMENT_ALIAS must differ from CANDIDATE_ALIAS: the candidate alias moves "
                "before validation, the served one only after a promotion",
                key="deployment_alias",
            )
        return cls(registry, alias, canary_alias=settings.deployment_canary_alias,
                   traffic_tag=settings.deployment_traffic_tag)

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

    def _alias(self, model: str, alias: str) -> str | None:
        try:
            return self.registry.get_version_by_alias(model, alias)
        except ModelNotFoundError:
            return None

    def set_traffic(self, model: str, *, stable: DeploymentTarget,
                    candidate: DeploymentTarget | None, percent: int) -> None:
        if self._alias(model, self.alias) != stable.version:
            self.registry.set_alias(model, self.alias, stable.version)
        if candidate is None or percent == 0:
            if self._alias(model, self.canary_alias) is not None:
                self.registry.delete_alias(model, self.canary_alias)
            return
        self.registry.set_version_tags(model, candidate.version, {self.traffic_tag: str(percent)})
        self.registry.set_alias(model, self.canary_alias, candidate.version)

    def traffic(self, model: str) -> TrafficSplit:
        stable = self._alias(model, self.alias)
        candidate = self._alias(model, self.canary_alias)
        if candidate is None:
            return TrafficSplit(model=model, stable=stable, candidate=None, percent=0)
        tag = self.registry.get_version(model, candidate).tags.get(self.traffic_tag, "0")
        percent = int(tag) if tag.isdigit() else 0
        return TrafficSplit(model=model, stable=stable, candidate=candidate, percent=percent)


def _build(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return RegistryAliasDeployment.from_settings(settings, registry)


SPEC = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="registry-alias",
        description="serving follows a registry alias (DEPLOYMENT_ALIAS, default LIVE_ALIAS)",
        features=frozenset({"undeploy", "instant", "traffic_split"}),
        config_keys=("deployment_alias", "live_alias", "candidate_alias",
                     "deployment_canary_alias", "deployment_traffic_tag"),
    ),
    factory=_build,
)
