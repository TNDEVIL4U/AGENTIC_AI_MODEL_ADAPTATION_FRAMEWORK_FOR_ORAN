"""Deployment, dataset, CDC source, job executor, job queue, notification sink and LLM ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, runtime_checkable

if TYPE_CHECKING:
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


@dataclass(frozen=True)
class SourceStat:
    """What a dataset adapter reports about one stored object, without reading its content.

    ``fingerprint`` changes whenever the object's bytes change (an ETag, an object generation,
    size plus modification time); None when the store offers nothing of the kind, in which case
    readers verify the content hash instead. ``pinned_uri`` names this exact object version
    when the store keeps versions (an S3 ``versionId``, a GCS ``generation``), so later writes
    to the same key do not change what the reference reads; otherwise it is the URI as given.
    """

    uri: str
    pinned_uri: str
    fingerprint: str | None
    size_bytes: int | None = None


@runtime_checkable
class DatasetPort(Protocol):
    """Byte access to data stored outside the framework's database (docs/adapters/dataset.md).

    A data version registered by reference keeps only a URI; reading it goes through the
    adapter whose ``schemes`` include the URI's scheme. ``check`` refuses (without I/O) a URI
    outside the adapter's configured allow-list with DataSourceNotAllowedError. ``stat`` and
    ``open`` raise DatasetNotFoundError for a missing object, DataSourceUnavailableError when
    the store cannot be reached, and DataTooLargeError past DATASET_MAX_SOURCE_BYTES. ``open``
    returns a seekable binary file the caller closes; parsing the format is not the adapter's
    concern (oran_adapt.datastore.formats)."""

    schemes: frozenset[str]

    def check(self, uri: str) -> None: ...

    def stat(self, uri: str) -> SourceStat: ...

    def open(self, uri: str) -> BinaryIO: ...


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

    Supervision: while the attempt runs, the executor calls ``on_tick()`` in the calling
    process every ``tick_s`` seconds. It returns None to go on, or the error to stop with (a
    cancel request, a lost lease, a drain): the executor then stops the attempt as it would on
    a timeout and raises that error.
    """

    job_id: str
    timeout_s: float
    run: Callable[[dict[str, Any]], dict[str, Any]]
    payload: dict[str, Any]
    on_abandoned: Callable[[], None] | None = None
    tick_s: float | None = None
    on_tick: Callable[[], Exception | None] | None = None


@runtime_checkable
class JobExecutorPort(Protocol):
    """Runs a job attempt within ``call.timeout_s``; raises JobTimeoutError past it.

    ``in_process`` is True when ``run`` executes in this process, so the payload may carry live
    objects (a session factory, in-process fakes) that could not cross a process boundary."""

    @property
    def in_process(self) -> bool: ...

    def execute(self, call: JobCall) -> dict[str, Any]: ...


@dataclass(frozen=True)
class QueuedJob:
    """A job ready to run, as announced to a JobQueuePort. The database row is the job; this is
    only what a broker needs to route the wake-up: the worker class picks the queue, the
    priority and the attempt number may shape the message."""

    job_id: str
    worker_class: str
    tenant: str
    priority: int
    attempt: int


@runtime_checkable
class JobQueuePort(Protocol):
    """Wakes a worker for a queued job (docs/adapters/job_queue.md).

    The job queue itself is the ``adaptation_job`` table: a job is QUEUED there before
    ``publish`` is called, and whichever worker claims it first (a conditional update that only
    one can win) runs it, so a message delivered twice, late or never cannot run a job twice or
    lose it; the reaper publishes again what stays unclaimed. ``publish`` hands ``job`` to the
    broker and raises JobQueueUnavailableError when the broker cannot be reached. The worker a
    message reaches runs ``oran_adapt.orchestrator.worker.run_job_by_id(job_id)``.

    ``runs_inline`` is True only for the development adapter: the submitting call then runs
    the job itself before it returns. ``ping`` checks the broker without publishing."""

    @property
    def runs_inline(self) -> bool: ...

    def ping(self) -> None: ...

    def publish(self, job: QueuedJob) -> None: ...


@dataclass(frozen=True)
class OutboundMessage:
    """One event on its way to one sink (docs/adapters/notification.md).

    ``envelope`` is the CloudEvents 1.0 JSON event (``id``, ``type``, ``source``, ``subject``,
    ``time``, ``data``); ``body`` is its canonical serialization, byte for byte what
    ``headers`` signs. ``headers`` carries ``webhook-id`` (the event id, stable across retries,
    for receiver-side deduplication), ``webhook-timestamp`` and ``webhook-signature`` (HMAC,
    oran_adapt.notifications.signing) when signing keys are configured. ``event_type`` is the
    short type (``job.failed``) the sink filters use."""

    event_id: str
    event_type: str
    subject: str
    envelope: dict[str, Any]
    body: bytes
    headers: dict[str, str]
    attempt: int = 1


@runtime_checkable
class NotificationPort(Protocol):
    """A notification sink: where the outbox dispatcher (oran_adapt.notifications) delivers
    events. ``send`` returns once the sink has accepted the message and raises
    NotificationDeliveryError otherwise, with ``retryable`` saying whether to try again; no
    vendor exception crosses the port. Delivery is at least once: a message may be sent again
    after a crash, always with the same ``event_id``. ``ping`` checks the sink is reachable
    without sending anything, raising NotificationDeliveryError when it is not."""

    def ping(self) -> None: ...

    def send(self, message: OutboundMessage) -> None: ...


@runtime_checkable
class LLMPort(Protocol):
    """A single-turn text completion. Implementations raise LlmUnavailableError on any failure
    (auth, network, timeout, provider error) - no provider SDK exception crosses this port."""

    def complete(self, *, system: str, prompt: str) -> str: ...
