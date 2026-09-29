"""Deployment adapters ``webhook`` and ``bentoml``: any serving system that implements the
framework's small HTTP deployment contract (docs/adapters/deployment.md):

- ``POST {base}/deploy`` with ``{"model", "version", "source"}`` (``version`` null = undeploy)
  answers 2xx once the request is accepted, 4xx when it is refused (the body says why);
- ``GET {base}/status?model=<model>`` answers ``{"version", "ready", "failed", "detail"}``,
  read from what the server actually serves;
- ``GET {base}/health`` answers 2xx when the server is up.

``bentoml`` is the same contract served by a BentoML service built from
``templates/bentoml-service`` (paths under ``/oran``).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import httpx

from oran_adapt.adapters.deployment._common import HttpApi, StaticToken, parse_status, required
from oran_adapt.core.errors import DeploymentError
from oran_adapt.ports import AdapterSpec, Capability, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.ports import DeploymentPort, ModelRegistryPort


class WebhookDeployment:
    def __init__(self, api: HttpApi) -> None:
        self.api = api

    @classmethod
    def from_settings(cls, settings: Settings, *, url_key: str, token_key: str,
                      service: str, suffix: str = "") -> WebhookDeployment:
        url = required(getattr(settings, url_key), url_key).rstrip("/") + suffix
        return cls(
            HttpApi(
                url,
                service=service,
                http_factory=partial(httpx.Client, timeout=settings.deployment_http_timeout_s),
                token=StaticToken(getattr(settings, token_key)),
            )
        )

    def ping(self) -> None:
        response = self.api.call("GET", "/health")
        if response.status_code >= 400:
            raise DeploymentError(
                f"{self.api.service} health check answered HTTP {response.status_code}",
                cause=response.text[:500],
            )

    def status(self, model: str) -> DeploymentState:
        response = self.api.call("GET", "/status", params={"model": model})
        if response.status_code == 404:
            return DeploymentState(model=model, version=None, ready=True)
        if response.status_code >= 400:
            raise DeploymentError(
                f"{self.api.service} refused the status request (HTTP {response.status_code})",
                model=model,
                cause=response.text[:500],
            )
        return parse_status(model, response.json(), self.api.service)

    def _post(self, model: str, version: str | None, source: str | None) -> None:
        response = self.api.call(
            "POST", "/deploy", json={"model": model, "version": version, "source": source}
        )
        if response.status_code >= 400:
            raise DeploymentError(
                f"{self.api.service} refused the deployment (HTTP {response.status_code})",
                model=model,
                version=version,
                cause=response.text[:500],
            )

    def deploy(self, target: DeploymentTarget) -> None:
        self._post(target.model, target.version, target.source)

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if previous is None:
            self._post(model, None, None)
        else:
            self._post(model, previous.version, previous.source)


def _webhook(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return WebhookDeployment.from_settings(
        settings, url_key="deployment_webhook_url", token_key="deployment_webhook_token",
        service="the deployment webhook",
    )


def _bentoml(settings: Settings, registry: ModelRegistryPort) -> DeploymentPort:
    return WebhookDeployment.from_settings(
        settings, url_key="bentoml_url", token_key="bentoml_token",
        service="the BentoML service", suffix="/oran",
    )


WEBHOOK = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="webhook",
        description="any serving system implementing the POST /deploy + GET /status contract",
        features=frozenset({"undeploy"}),
        config_keys=(
            "deployment_webhook_url", "deployment_webhook_token", "deployment_http_timeout_s",
        ),
        required_keys=("deployment_webhook_url",),
    ),
    factory=_webhook,
)

BENTOML = AdapterSpec(
    capability=Capability(
        port="deployment",
        adapter="bentoml",
        description="a BentoML service from templates/bentoml-service (webhook contract, /oran)",
        features=frozenset({"undeploy"}),
        config_keys=("bentoml_url", "bentoml_token", "deployment_http_timeout_s"),
        required_keys=("bentoml_url",),
    ),
    factory=_bentoml,
)
