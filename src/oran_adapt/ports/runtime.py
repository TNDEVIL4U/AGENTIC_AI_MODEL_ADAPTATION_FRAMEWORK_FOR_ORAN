"""Deployment, dataset, CDC source, job executor, notification and LLM ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class DeploymentTarget:
    """One registered model version to put into service.

    ``source`` is the registry's reference to the artifact (``ModelVersion.source``: an MLflow
    URI, a model package ARN, a Vertex model resource), None when the registry has none.
    ``artifact_dir`` is a local, checksum-verified copy of the artifact when the caller has one
    (adapters that stage files into a model repository need it), None otherwise."""

    model: str
    version: str
    source: str | None = None
    artifact_dir: str | None = None


@dataclass(frozen=True)
class DeploymentState:
    """What the serving system reports for one model, read back from the system itself.

    ``version`` is the version it serves or is moving to (None: nothing deployed). ``ready``
    means the system has settled at ``version``: every replica serves it. ``failed`` means the
    system gave up on the last request (a failed rollout); ``detail`` says why, for operators.
    """

    model: str
    version: str | None
    ready: bool
    failed: bool = False
    detail: str = ""


@runtime_checkable
class DeploymentPort(Protocol):
    """Where a model version serves traffic (docs/adapters/deployment.md).

    ``deploy`` and ``restore`` return once the serving system has accepted the request, not
    once it is serving: callers poll ``status`` until it reads back ready at the version asked
    for (``oran_adapt.registry.deployment.Deployer``). From the moment ``deploy`` returns,
    ``status`` must not report the old version as settled. Adapters raise
    DeploymentUnavailableError when the system cannot be reached, DeploymentError when it
    refuses the request, and ModelNotFoundError when the registry holds no such version."""

    def ping(self) -> None: ...

    def status(self, model: str) -> DeploymentState: ...

    def deploy(self, target: DeploymentTarget) -> None: ...

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        """Put ``previous`` back (None = undeploy) after a failed or rolled-back deployment."""
        ...


@runtime_checkable
class DatasetPort(Protocol):
    """Read access to versioned datasets for analysis and training."""

    def load_version_frame(self, session: Session, data_version_id: int) -> pd.DataFrame:
        """One stored data version as a frame (``observed_at`` plus the payload columns)."""
        ...


@runtime_checkable
class CdcSourcePort(Protocol):
    """A stream of change events. ``fetch`` returns the events and an opaque position token;
    ``name`` identifies the stream (it keys the stored offset)."""

    name: str

    def fetch(self, session: Session, limit: int) -> tuple[list[Any], str | None]: ...

    def ack(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class JobCall:
    """One attempt of one adaptation job, as handed to a JobExecutorPort.

    The executor calls ``run(payload)``, which returns a JSON-safe dict or raises. Executors
    that run it in another process need both to pickle (``run`` a module-level function).
    ``on_abandoned`` fires once a timed-out attempt that could not be stopped has finished.
    """

    job_id: str
    timeout_s: float
    run: Callable[[dict[str, Any]], dict[str, Any]]
    payload: dict[str, Any]
    on_abandoned: Callable[[], None] | None = None


@runtime_checkable
class JobExecutorPort(Protocol):
    """Runs a job attempt within ``call.timeout_s``; raises JobTimeoutError past it.

    ``in_process`` is True when ``run`` executes in this process, so the payload may carry live
    objects (a session factory, in-process fakes) that could not cross a process boundary."""

    @property
    def in_process(self) -> bool: ...

    def execute(self, call: JobCall) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Notification:
    event: str
    subject: str
    detail: dict[str, Any]


@runtime_checkable
class NotificationPort(Protocol):
    """Fire-and-forget operator notifications. Delivery failures are logged and counted by the
    adapter, never raised into the job that triggered them."""

    def notify(self, notification: Notification) -> None: ...


@runtime_checkable
class LLMPort(Protocol):
    """A single-turn text completion. Implementations raise LlmUnavailableError on any failure
    (auth, network, timeout, provider error) - no provider SDK exception crosses this port."""

    def complete(self, *, system: str, prompt: str) -> str: ...
