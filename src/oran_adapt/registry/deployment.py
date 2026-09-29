"""Putting a model version into service and proving it got there.

``Deployer`` wraps the configured ``DeploymentPort`` adapter. ``rollout`` asks the serving
system to serve a version, then reads the system's own state back until it reports that version
ready (post-deploy read-back is mandatory: an accepted request is not a deployment). If the
system refuses, reports a failure, settles at another version, or does not settle within
DEPLOYMENT_TIMEOUT_S, the previous version is put back and read back too, and DeploymentError
says whether that restore held.

Promotion (``registry.promotion``) calls ``rollout`` after moving the live alias, and undoes
both when either fails, so LIVE and what serves traffic never disagree after a failed move.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from oran_adapt.core import metrics
from oran_adapt.core.errors import AdaptationError, DeploymentError
from oran_adapt.core.logging import log_event
from oran_adapt.ports import DeploymentPort, DeploymentState, DeploymentTarget

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

logger = logging.getLogger(__name__)


class Deployer:
    def __init__(
        self,
        port: DeploymentPort,
        *,
        backend: str,
        timeout_s: float,
        poll_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.port = port
        self.backend = backend
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._clock = clock
        self._sleep = sleep

    @classmethod
    def from_settings(cls, port: DeploymentPort, settings: Settings) -> Deployer:
        return cls(
            port,
            backend=settings.deployment_backend,
            timeout_s=settings.deployment_timeout_s,
            poll_s=settings.deployment_poll_s,
        )

    def serving(self, model: str) -> str | None:
        """The version the serving system reports for ``model`` (None: nothing deployed)."""
        return self.port.status(model).version

    def _wait(self, model: str, version: str | None, *, fail_on_other: bool) -> DeploymentState:
        """Poll ``status`` until it reads back settled at ``version``. Raises DeploymentError
        on a reported failure, on the timeout, or (``fail_on_other``) when the system settles at
        a different version, which means it rejected or rolled back the request."""
        deadline = self._clock() + self.timeout_s
        while True:
            state = self.port.status(model)
            if state.ready and state.version == version:
                return state
            if state.failed:
                raise DeploymentError(
                    f"the serving system reported a failed rollout of version {version}",
                    model=model,
                    version=version,
                    serving=state.version,
                    detail=state.detail,
                )
            if fail_on_other and state.ready and state.version != version:
                raise DeploymentError(
                    f"the serving system settled at version {state.version}, not {version}",
                    model=model,
                    version=version,
                    serving=state.version,
                    detail=state.detail,
                )
            if self._clock() >= deadline:
                raise DeploymentError(
                    f"version {version} did not read back as serving within "
                    f"{self.timeout_s:g}s (DEPLOYMENT_TIMEOUT_S)",
                    model=model,
                    version=version,
                    serving=state.version,
                    ready=state.ready,
                    detail=state.detail,
                )
            self._sleep(self.poll_s)

    def rollout(self, target: DeploymentTarget, previous: DeploymentTarget | None) -> DeploymentState:
        """Serve ``target`` and read it back. On any failure put ``previous`` back (None =
        undeploy), read that back, and raise DeploymentError with ``restored`` in its context."""
        started = self._clock()
        try:
            self.port.deploy(target)
            state = self._wait(target.model, target.version, fail_on_other=True)
        except AdaptationError as exc:
            restored = self.revert(target.model, previous)
            metrics.DEPLOYMENTS.labels(backend=self.backend, outcome="failed").inc()
            raise DeploymentError(
                f"deploying version {target.version} failed; "
                + (
                    f"restored {previous.version if previous else 'nothing deployed'}"
                    if restored
                    else "the previous version could not be restored"
                ),
                model=target.model,
                version=target.version,
                previous=previous.version if previous else None,
                restored=restored,
                cause=exc.message,
                **{k: v for k, v in exc.context.items() if k in ("serving", "detail")},
            ) from exc
        metrics.DEPLOYMENTS.labels(backend=self.backend, outcome="ok").inc()
        log_event(
            logger,
            "deployment read back as serving",
            backend=self.backend,
            model=target.model,
            version=target.version,
            seconds=round(self._clock() - started, 3),
        )
        return state

    def revert(self, model: str, previous: DeploymentTarget | None) -> bool:
        """Put ``previous`` back and read it back. Returns whether that held; failures are
        logged (the caller is already reporting the error that made the revert necessary)."""
        version = previous.version if previous else None
        try:
            self.port.restore(model, previous)
            self._wait(model, version, fail_on_other=False)
        except AdaptationError as exc:
            log_event(
                logger,
                "could not restore the previous deployment",
                level=logging.ERROR,
                backend=self.backend,
                model=model,
                previous=version,
                cause=exc.message,
            )
            return False
        log_event(logger, "previous deployment restored", backend=self.backend, model=model,
                  previous=version)
        return True
