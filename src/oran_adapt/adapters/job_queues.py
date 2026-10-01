"""JobQueuePort adapters: how a worker is woken for a queued job (docs/adapters/job_queue.md).

The queue itself is the ``adaptation_job`` table (orchestrator.worker); these adapters only
deliver wake-ups, so a lost, late or duplicated message cannot lose or double a job.

``database`` (default): no broker. Workers (``oran-adapt worker run``) poll the table.
``inline``: the submitting call runs the job itself (tests and development only).
``celery``: a task named JOB_QUEUE_CELERY_TASK on queue <JOB_QUEUE_NAME_PREFIX><class>; a
    Celery worker registers that task to call orchestrator.worker.run_job_by_id.
``rq``: a Redis Queue per class running orchestrator.worker.run_job_by_id.
``kubernetes``: a batch/v1 Job per attempt running ``oran-adapt worker run-job --job-id``.

The broker SDKs (celery, redis/rq) are imported only when their adapter is built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import (
    ConfigurationError,
    DeploymentUnavailableError,
    JobQueueUnavailableError,
)
from oran_adapt.ports import AdapterSpec, Capability, QueuedJob

if TYPE_CHECKING:
    from oran_adapt.adapters.deployment._common import HttpApi
    from oran_adapt.core.config import Settings

RUN_JOB_FUNCTION = "oran_adapt.orchestrator.worker.run_job_by_id"
_K8S_NAME_MAX = 63


def _missing_sdk(adapter: str, package: str, extra: str) -> ConfigurationError:
    return ConfigurationError(
        f"JOB_QUEUE_BACKEND={adapter} needs the {package} package "
        f"(pip install 'oran-adapt[{extra}]')",
        adapter=adapter,
    )


def _required(value: Any, key: str, adapter: str) -> Any:
    if value is None or value == "":
        raise ConfigurationError(
            f"JOB_QUEUE_BACKEND={adapter} needs {key.upper()} to be set", key=key
        )
    return value


def queue_name(settings: Settings, worker_class: str) -> str:
    return f"{settings.job_queue_name_prefix}{worker_class}"


class DatabaseQueue:
    """Workers poll the job table; publishing has nothing to do."""

    runs_inline = False

    def ping(self) -> None:
        return None

    def publish(self, job: QueuedJob) -> None:
        return None


class InlineQueue(DatabaseQueue):
    """The submitting call runs the job (orchestrator.worker.Worker.run_to_settled)."""

    runs_inline = True


class CeleryQueue:
    """Sends task ``task`` with the job id to the class's queue. ``app`` is a Celery app."""

    runs_inline = False

    def __init__(self, app: Any, settings: Settings, errors: tuple[type[BaseException], ...]):
        self.app = app
        self.settings = settings
        self.errors = errors

    def ping(self) -> None:
        try:
            with self.app.connection_for_write() as conn:
                conn.ensure_connection(max_retries=1)
        except self.errors as exc:
            raise JobQueueUnavailableError("the Celery broker is not reachable",
                                           backend="celery", cause=str(exc)) from exc

    def publish(self, job: QueuedJob) -> None:
        try:
            self.app.send_task(
                self.settings.job_queue_celery_task,
                args=[job.job_id],
                queue=queue_name(self.settings, job.worker_class),
                priority=job.priority,
            )
        except self.errors as exc:
            raise JobQueueUnavailableError("the Celery broker refused the job",
                                           backend="celery", job_id=job.job_id,
                                           cause=str(exc)) from exc


class RqQueue:
    """Enqueues run_job_by_id(job_id) on the class's Redis Queue."""

    runs_inline = False

    def __init__(self, redis: Any, queue_factory: Any, settings: Settings,
                 errors: tuple[type[BaseException], ...]):
        self.redis = redis
        self.queue_factory = queue_factory
        self.settings = settings
        self.errors = errors

    def ping(self) -> None:
        try:
            self.redis.ping()
        except self.errors as exc:
            raise JobQueueUnavailableError("Redis is not reachable", backend="rq",
                                           cause=str(exc)) from exc

    def publish(self, job: QueuedJob) -> None:
        try:
            queue = self.queue_factory(queue_name(self.settings, job.worker_class),
                                       connection=self.redis)
            queue.enqueue(RUN_JOB_FUNCTION, job.job_id,
                          job_timeout=int(self.settings.job_timeout_s
                                          + self.settings.job_drain_timeout_s + 60))
        except self.errors as exc:
            raise JobQueueUnavailableError("Redis refused the job", backend="rq",
                                           job_id=job.job_id, cause=str(exc)) from exc


class KubernetesJobQueue:
    """Creates a batch/v1 Job per attempt; a 409 (that attempt's Job already exists) is fine."""

    runs_inline = False

    def __init__(self, api: HttpApi, settings: Settings) -> None:
        self.api = api
        self.settings = settings

    def _jobs_path(self) -> str:
        return f"/apis/batch/v1/namespaces/{self.settings.k8s_namespace}/jobs"

    def ping(self) -> None:
        try:
            response = self.api.call("GET", self._jobs_path(), params={"limit": 1})
        except DeploymentUnavailableError as exc:
            raise JobQueueUnavailableError(exc.message, backend="kubernetes",
                                           **exc.context) from exc
        if response.status_code >= 400:
            raise JobQueueUnavailableError(
                f"the Kubernetes API answered HTTP {response.status_code}",
                backend="kubernetes", cause=response.text[:500],
            )

    def manifest(self, job: QueuedJob) -> dict[str, Any]:
        s = self.settings
        name = f"{s.k8s_name_prefix}oran-job-{job.job_id}-a{job.attempt + 1}".lower()
        container: dict[str, Any] = {
            "name": "worker",
            "image": s.job_queue_k8s_image,
            "command": ["oran-adapt", "worker", "run-job", "--job-id", job.job_id],
        }
        if s.job_queue_k8s_env_secret:
            container["envFrom"] = [{"secretRef": {"name": s.job_queue_k8s_env_secret}}]
        pod: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
        if s.job_queue_k8s_service_account:
            pod["serviceAccountName"] = s.job_queue_k8s_service_account
        overrides = dict(s.job_queue_k8s_class_pods.get(job.worker_class, {}))
        resources = overrides.pop("resources", None)
        if resources is not None:
            container["resources"] = resources
        pod.update(overrides)
        labels = {"app.kubernetes.io/managed-by": "oran-adapt",
                  "oran.io/worker-class": job.worker_class}
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name[:_K8S_NAME_MAX].rstrip("-"), "labels": labels,
                         "annotations": {"oran.io/job-id": job.job_id}},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": int(s.job_timeout_s + s.job_drain_timeout_s + 60),
                "ttlSecondsAfterFinished": s.job_queue_k8s_ttl_after_finished_s,
                "template": {"metadata": {"labels": labels}, "spec": pod},
            },
        }

    def publish(self, job: QueuedJob) -> None:
        try:
            response = self.api.call("POST", self._jobs_path(), json=self.manifest(job))
        except DeploymentUnavailableError as exc:
            raise JobQueueUnavailableError(exc.message, backend="kubernetes",
                                           job_id=job.job_id, **exc.context) from exc
        if response.status_code == 409 or response.status_code < 300:
            return
        raise JobQueueUnavailableError(
            f"the Kubernetes API refused the Job with HTTP {response.status_code}",
            backend="kubernetes", job_id=job.job_id, cause=response.text[:500],
        )


def _database(settings: Settings) -> DatabaseQueue:
    return DatabaseQueue()


def _inline(settings: Settings) -> InlineQueue:
    return InlineQueue()


def _celery(settings: Settings) -> CeleryQueue:
    try:
        from celery import Celery
        from kombu.exceptions import OperationalError
    except ImportError:
        raise _missing_sdk("celery", "celery", "celery") from None
    broker = _required(settings.job_queue_celery_broker_url, "job_queue_celery_broker_url",
                       "celery")
    app = Celery("oran_adapt", broker=broker.get_secret_value())
    return CeleryQueue(app, settings, (OperationalError, OSError))


def _rq(settings: Settings) -> RqQueue:
    try:
        from redis import Redis
        from redis.exceptions import RedisError
        from rq import Queue
    except ImportError:
        raise _missing_sdk("rq", "rq", "rq") from None
    url = _required(settings.job_queue_rq_redis_url, "job_queue_rq_redis_url", "rq")
    return RqQueue(Redis.from_url(url.get_secret_value()), Queue, settings,
                   (RedisError, OSError))


def _kubernetes(settings: Settings) -> KubernetesJobQueue:
    from oran_adapt.adapters.deployment.kubernetes import kube_api

    _required(settings.job_queue_k8s_image, "job_queue_k8s_image", "kubernetes")
    return KubernetesJobQueue(kube_api(settings), settings)


DATABASE = AdapterSpec(
    capability=Capability(
        port="job_queue",
        adapter="database",
        description="no broker: workers poll the job table",
        features=frozenset({"durable"}),
        config_keys=("job_poll_interval_s",),
    ),
    factory=_database,
)

INLINE = AdapterSpec(
    capability=Capability(
        port="job_queue",
        adapter="inline",
        description="the submitting call runs the job itself (tests, development)",
        features=frozenset({"development_only", "inline"}),
    ),
    factory=_inline,
)

CELERY = AdapterSpec(
    capability=Capability(
        port="job_queue",
        adapter="celery",
        description="a Celery task per job on queue <prefix><worker class>",
        features=frozenset({"broker", "priority"}),
        config_keys=("job_queue_celery_broker_url", "job_queue_celery_task",
                     "job_queue_name_prefix"),
        required_keys=("job_queue_celery_broker_url",),
        distributions=("celery",),
    ),
    factory=_celery,
)

RQ = AdapterSpec(
    capability=Capability(
        port="job_queue",
        adapter="rq",
        description="a Redis Queue job per attempt on queue <prefix><worker class>",
        features=frozenset({"broker"}),
        config_keys=("job_queue_rq_redis_url", "job_queue_name_prefix"),
        required_keys=("job_queue_rq_redis_url",),
        distributions=("rq",),
    ),
    factory=_rq,
)

KUBERNETES = AdapterSpec(
    capability=Capability(
        port="job_queue",
        adapter="kubernetes",
        description="a Kubernetes batch/v1 Job per attempt, pod shape per worker class",
        features=frozenset({"isolated", "per_class_resources"}),
        config_keys=("job_queue_k8s_image", "job_queue_k8s_env_secret",
                     "job_queue_k8s_service_account", "job_queue_k8s_class_pods",
                     "job_queue_k8s_ttl_after_finished_s", "k8s_api_url", "k8s_namespace",
                     "k8s_token", "k8s_token_file", "k8s_ca_file", "k8s_name_prefix"),
        required_keys=("job_queue_k8s_image", "k8s_api_url"),
    ),
    factory=_kubernetes,
)
