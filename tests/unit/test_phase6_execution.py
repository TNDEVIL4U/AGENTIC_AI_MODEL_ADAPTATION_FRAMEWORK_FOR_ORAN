"""Phase 6: the real execution layer. The API only queues a job; workers claim it under a lease,
run it on the job executor with a checkpoint supervisor, and record exactly one outcome. The
reaper requeues jobs whose worker died, quarantines poison jobs and times out overdue ones.
Cancellation, drain, deadlines, tenant concurrency, priorities, worker classes, the five queue
adapters (conformance suite) and migration 0008's rollback.

Every attempt here runs on the ``thread`` executor with in-process stand-ins for the pipeline;
killing real worker processes is scripts/acceptance/phase6.py's job."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, update

from oran_adapt import plugins
from oran_adapt.adapters.deployment._common import HttpApi, StaticToken
from oran_adapt.adapters.job_queues import (
    CeleryQueue,
    DatabaseQueue,
    InlineQueue,
    KubernetesJobQueue,
    RqQueue,
)
from oran_adapt.api.app import create_app
from oran_adapt.conformance import job_queue as conformance
from oran_adapt.core.enums import JobStatus
from oran_adapt.core.errors import (
    ConfigurationError,
    JobLeaseLostError,
    JobNotCancellableError,
    JobQueueUnavailableError,
    JobWorkerLostError,
)
from oran_adapt.core.processes import descendants, kill_tree, pid_alive
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.migrate import downgrade_to_base, upgrade_to_head
from oran_adapt.db.models import AdaptationEvent, AdaptationJob, JobSlot, ModelLock
from oran_adapt.orchestrator import jobs as jobs_module
from oran_adapt.orchestrator import worker as worker_module
from oran_adapt.orchestrator.jobs import request_cancel, submit_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.orchestrator.worker import Worker, claim, reap
from oran_adapt.ports import JobQueuePort, QueuedJob

RUNNING = threading.Event()
RELEASE = threading.Event()


def _ok_pipeline(session, event, settings, *, registry, llm_client, workdir):
    return JobResult(model_id=event.model_id, outcome="NO_ACTION", reason="ok")


def _slow_pipeline(session, event, settings, *, registry, llm_client, workdir):
    """Runs until the test releases it (at most 10 s): a job still working when stopped."""
    RUNNING.set()
    RELEASE.wait(10)
    return JobResult(model_id=event.model_id, outcome="NO_ACTION", reason="released")


@pytest.fixture(autouse=True)
def _release_slow_pipelines():
    RUNNING.clear()
    RELEASE.clear()
    yield
    RELEASE.set()


@pytest.fixture
def qsettings(migrated_settings):
    """Queue mode (workers claim jobs), thread executor, fast checkpoints."""
    return migrated_settings.model_copy(update={
        "job_queue_backend": "database",
        "job_execution_mode": "thread",
        "job_heartbeat_s": 0.1,
        "job_lease_ttl_s": 1.0,
        "job_retry_backoff_s": 0.0,
        "job_poll_interval_s": 0.05,
    })


@pytest.fixture
def sf(qsettings):
    return make_session_factory(create_db_engine(qsettings.database_url))


def _submit(sf, settings, model_id: str, event_id: str | None = None, *, queue=None,
            **event: Any):
    return submit_adaptation_job(
        sf, DriftEvent(model_id=model_id, event_id=event_id or f"evt-{model_id}", **event),
        settings, registry=None, llm_client=None, workdir=settings.artifact_workdir,
        queue=queue or DatabaseQueue(),
    )


def _worker(sf, settings, **kw: Any) -> Worker:
    return Worker(sf, settings, registry=None, llm_client=None,
                  workdir=settings.artifact_workdir, queue=DatabaseQueue(), **kw)


def _job(sf, job_id: str) -> AdaptationJob:
    with session_scope(sf) as session:
        job = session.scalar(select(AdaptationJob).where(AdaptationJob.job_id == job_id))
        assert job is not None
        session.expunge(job)
        return job


def _path(sf, job_id: str) -> list[str]:
    with session_scope(sf) as session:
        return list(session.scalars(
            select(AdaptationEvent.to_status).where(AdaptationEvent.job_id == job_id)
            .order_by(AdaptationEvent.id)
        ))


def _expire_lease(sf, job_id: str) -> None:
    with session_scope(sf) as session:
        session.execute(update(AdaptationJob).where(AdaptationJob.job_id == job_id).values(
            lease_expires_at=jobs_module._now() - timedelta(seconds=5)))


# ---- migration -------------------------------------------------------------------------------
@pytest.mark.smoke
def test_migration_0008_up_and_down(settings) -> None:
    upgrade_to_head(settings.database_url)
    engine = create_db_engine(settings.database_url)
    columns = {c["name"] for c in inspect(engine).get_columns("adaptation_job")}
    assert {"lease_token", "worker_class", "tenant", "quarantined"} <= columns
    assert "job_slot" in inspect(engine).get_table_names()
    engine.dispose()
    downgrade_to_base(settings.database_url)
    engine = create_db_engine(settings.database_url)
    assert "job_slot" not in inspect(engine).get_table_names()
    engine.dispose()
    upgrade_to_head(settings.database_url)


# ---- submit, claim, run ----------------------------------------------------------------------
def test_submit_only_queues_and_a_worker_runs_the_job(sf, qsettings, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _ok_pipeline)
    queued = _submit(sf, qsettings, "m-queue")
    assert queued.status == JobStatus.QUEUED
    assert queued.attempt == 0 and queued.worker_class == "default" and queued.tenant == "default"

    assert _worker(sf, qsettings).run(once=True) == 1
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.attempt == 1 and job.lease_token is None
    assert _path(sf, queued.job_id) == ["RECEIVED", "QUEUED", "VALIDATING", "DATA_PREPARING",
                                        "COMPLETED"]
    with session_scope(sf) as session:
        assert session.get(ModelLock, "m-queue") is None


def test_only_one_worker_can_claim_a_job(sf, qsettings) -> None:
    queued = _submit(sf, qsettings, "m-race")
    first = claim(sf, qsettings, owner="w1", job_id=queued.job_id)
    second = claim(sf, qsettings, owner="w2", job_id=queued.job_id)
    assert first is not None and second is None
    assert _job(sf, queued.job_id).lease_owner == "w1"


def test_duplicate_event_from_two_replicas_makes_one_job(qsettings) -> None:
    """Two API replicas (separate engines, same database) receive the same event at once."""
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def replica() -> None:
        factory = make_session_factory(create_db_engine(qsettings.database_url))
        barrier.wait()
        results.append(_submit(factory, qsettings, "m-dup", "evt-dup-1"))

    threads = [threading.Thread(target=replica) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 2
    assert sorted(r.duplicate for r in results) == [False, True]
    assert results[0].job_id == results[1].job_id
    sf = make_session_factory(create_db_engine(qsettings.database_url))
    with session_scope(sf) as session:
        assert session.scalar(select(func.count()).select_from(AdaptationJob)) == 1


def test_priority_then_age_decides_the_claim_order(sf, qsettings) -> None:
    low = _submit(sf, qsettings, "m-low", severity="LOW")
    critical = _submit(sf, qsettings, "m-critical", severity="CRITICAL")
    got = claim(sf, qsettings, owner="w")
    assert got is not None and got.job_id == critical.job_id
    got = claim(sf, qsettings, owner="w")
    assert got is not None and got.job_id == low.job_id


def test_a_job_runs_only_on_its_worker_class(sf, qsettings) -> None:
    gpu = qsettings.model_copy(update={"job_default_class": "gpu"})
    queued = _submit(sf, gpu, "m-gpu")
    assert queued.worker_class == "gpu"
    assert claim(sf, qsettings, owner="cpu-worker", classes=["default"]) is None
    got = claim(sf, qsettings, owner="gpu-worker", classes=["gpu"])
    assert got is not None and got.job_id == queued.job_id


def test_tenant_concurrency_limit_holds_across_claims(sf, qsettings, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _ok_pipeline)
    limited = qsettings.model_copy(update={"job_tenant_concurrency": 1})
    a = _submit(sf, limited, "m-t1")
    b = _submit(sf, limited, "m-t2")
    first = claim(sf, limited, owner="w1")
    assert first is not None and first.job_id == a.job_id and first.slot == 0
    assert claim(sf, limited, owner="w2") is None  # the tenant's one slot is taken
    _worker(sf, limited).run_claim(first)
    with session_scope(sf) as session:
        assert session.scalar(select(func.count()).select_from(JobSlot)) == 0
    second = claim(sf, limited, owner="w2")
    assert second is not None and second.job_id == b.job_id


# ---- worker death, reaper, poison ------------------------------------------------------------
def test_expired_lease_is_requeued_and_finished_once(sf, qsettings, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _ok_pipeline)
    queued = _submit(sf, qsettings, "m-dead")
    dead = claim(sf, qsettings, owner="dead-worker")
    assert dead is not None
    _expire_lease(sf, queued.job_id)  # the worker was killed: nobody renews its lease

    counts = reap(sf, qsettings, DatabaseQueue())
    assert counts["lease_expired"] == 1
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.QUEUED and job.lost_count == 1 and job.lease_token is None

    assert _worker(sf, qsettings).run(once=True) == 1
    # The dead worker's late report is fenced off: the job has exactly one outcome.
    _worker(sf, qsettings)._record(dead, JobStatus.FAILED, message="late report")
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.COMPLETED and job.attempt == 2
    path = _path(sf, queued.job_id)
    assert path.count("COMPLETED") == 1 and "FAILED" not in path


def test_poison_job_is_quarantined(sf, qsettings) -> None:
    poison = qsettings.model_copy(update={"job_poison_threshold": 2})
    queued = _submit(sf, poison, "m-poison")
    for _ in range(2):
        assert claim(sf, poison, owner="doomed") is not None
        _expire_lease(sf, queued.job_id)
        reap(sf, poison, DatabaseQueue())
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.FAILED and job.quarantined
    assert job.error["code"] == "JOB_QUARANTINED"
    assert claim(sf, poison, owner="w") is None
    with session_scope(sf) as session:
        assert session.get(ModelLock, "m-poison") is None


def test_worker_lost_while_registering_needs_reconciliation(sf, qsettings) -> None:
    queued = _submit(sf, qsettings, "m-reg")
    assert claim(sf, qsettings, owner="w") is not None
    with session_scope(sf) as session:
        session.execute(update(AdaptationJob).where(AdaptationJob.job_id == queued.job_id)
                        .values(status=JobStatus.REGISTERING.value))
    _expire_lease(sf, queued.job_id)
    reap(sf, qsettings, DatabaseQueue())
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "JOB_ABANDONED"
    assert job.error["context"]["needs_reconciliation"] is True


def test_worker_process_lost_mid_attempt_is_requeued(sf, qsettings, monkeypatch) -> None:
    class LosingExecutor:
        in_process = False

        def execute(self, call):
            raise JobWorkerLostError("job worker process exited with code -9 and no result",
                                     exitcode=-9)

    monkeypatch.setattr(worker_module, "build_job_executor", lambda s: LosingExecutor())
    queued = _submit(sf, qsettings, "m-lost")
    got = claim(sf, qsettings, owner="w")
    assert got is not None
    _worker(sf, qsettings).run_claim(got)
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.QUEUED and job.lost_count == 1 and job.lease_token is None


def test_stale_lease_holder_cannot_write(sf, qsettings) -> None:
    queued = _submit(sf, qsettings, "m-fence")
    assert claim(sf, qsettings, owner="w") is not None
    with pytest.raises(JobLeaseLostError):
        jobs_module._transition(sf, queued.job_id, settings=qsettings,
                                to_status=JobStatus.EVALUATING_VERSIONS, fence="not-the-token")


# ---- deadlines -------------------------------------------------------------------------------
def test_queued_job_past_its_deadline_times_out(sf, qsettings) -> None:
    short = qsettings.model_copy(update={"job_deadline_s": 0.05})
    queued = _submit(sf, short, "m-late")
    time.sleep(0.1)
    assert reap(sf, short, DatabaseQueue())["deadline"] == 1
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.TIMED_OUT and job.error["context"]["last_stage"] == "QUEUED"
    with session_scope(sf) as session:
        assert session.get(ModelLock, "m-late") is None


def test_running_job_is_stopped_at_its_deadline(sf, qsettings, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _slow_pipeline)
    short = qsettings.model_copy(update={"job_deadline_s": 0.8})
    queued = _submit(sf, short, "m-deadline")
    got = claim(sf, short, owner="w")
    assert got is not None
    started = time.monotonic()
    _worker(sf, short).run_claim(got)
    assert time.monotonic() - started < 3.0
    assert _job(sf, queued.job_id).status == JobStatus.TIMED_OUT


# ---- cancel and drain ------------------------------------------------------------------------
def test_cancel_queued_job_is_immediate(sf, qsettings) -> None:
    queued = _submit(sf, qsettings, "m-cq")
    response, immediate = request_cancel(sf, qsettings, queued.job_id, actor="op")
    assert immediate and response.status == JobStatus.CANCELLED and response.cancel_requested
    assert claim(sf, qsettings, owner="w") is None
    with session_scope(sf) as session:
        assert session.get(ModelLock, "m-cq") is None
    with pytest.raises(JobNotCancellableError):
        request_cancel(sf, qsettings, queued.job_id, actor="op")


def test_cancel_stops_a_running_job_within_the_checkpoint_interval(
    sf, qsettings, monkeypatch
) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _slow_pipeline)
    queued = _submit(sf, qsettings, "m-cr")
    got = claim(sf, qsettings, owner="w")
    assert got is not None
    requested: dict[str, float] = {}

    def cancel_when_running() -> None:
        RUNNING.wait(5)
        requested["at"] = time.monotonic()
        _, immediate = request_cancel(sf, qsettings, queued.job_id, actor="op")
        requested["immediate"] = float(immediate)

    canceller = threading.Thread(target=cancel_when_running)
    canceller.start()
    _worker(sf, qsettings).run_claim(got)
    stopped = time.monotonic()
    canceller.join(5)
    assert requested["immediate"] == 0.0  # running: a request, honoured at a checkpoint
    assert stopped - requested["at"] < qsettings.job_heartbeat_s * 5
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.CANCELLED and job.error["code"] == "JOB_CANCELLED"
    # A thread attempt cannot be killed: the model lock stays until it ends, then goes.
    RELEASE.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with session_scope(sf) as session:
            if session.get(ModelLock, "m-cr") is None:
                break
        time.sleep(0.05)
    with session_scope(sf) as session:
        assert session.get(ModelLock, "m-cr") is None


def test_drain_requeues_the_running_job_without_charging_it(sf, qsettings, monkeypatch) -> None:
    monkeypatch.setattr(jobs_module, "run_adaptation_job", _slow_pipeline)
    draining = qsettings.model_copy(update={"job_drain_timeout_s": 0.0})
    queued = _submit(sf, draining, "m-drain")
    got = claim(sf, draining, owner="w")
    assert got is not None
    worker = _worker(sf, draining)
    worker.drain()
    worker.run_claim(got)
    job = _job(sf, queued.job_id)
    assert job.status == JobStatus.QUEUED and job.attempt == 0 and job.lease_token is None
    assert worker.run() == 0  # a draining worker claims nothing


# ---- publishing ------------------------------------------------------------------------------
class FlakyQueue(DatabaseQueue):
    def __init__(self) -> None:
        self.down = True
        self.sent: list[str] = []

    def publish(self, job: QueuedJob) -> None:
        if self.down:
            raise JobQueueUnavailableError("broker down", backend="test")
        self.sent.append(job.job_id)


def test_unpublished_job_stays_queued_and_the_reaper_publishes_it(sf, qsettings) -> None:
    queue = FlakyQueue()
    queued = _submit(sf, qsettings, "m-pub", queue=queue)
    assert queued.status == JobStatus.QUEUED
    assert _job(sf, queued.job_id).published_at is None
    assert reap(sf, qsettings, queue)["published"] == 0
    queue.down = False
    assert reap(sf, qsettings, queue)["published"] == 1
    assert queue.sent == [queued.job_id]
    assert _job(sf, queued.job_id).published_at is not None
    assert reap(sf, qsettings, queue)["published"] == 0  # not again until it is stale


# ---- REST ------------------------------------------------------------------------------------
def test_rest_submit_list_and_cancel(qsettings) -> None:
    with TestClient(create_app(qsettings)) as api:
        body = {"model_id": "m-rest", "event_id": "evt-rest"}
        first = api.post("/api/v1/adaptation/events", json=body)
        assert first.status_code == 201 and first.json()["status"] == "QUEUED"
        again = api.post("/api/v1/adaptation/events", json=body)
        assert again.status_code == 200 and again.json()["duplicate"] is True
        listed = api.get("/api/v1/adaptation/jobs", params={"status": "QUEUED"}).json()
        assert listed["total"] == 1 and listed["items"][0]["job_id"] == first.json()["job_id"]
        job_id = first.json()["job_id"]
        cancelled = api.post(f"/api/v1/adaptation/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        assert api.post(f"/api/v1/adaptation/jobs/{job_id}/cancel").status_code == 409
        assert api.post("/api/v1/adaptation/jobs/nope/cancel").status_code == 404
        ports = api.get("/api/v1/capabilities").json()["ports"]
        assert ports["job_queue"]["selected"] == "database"


# ---- processes -------------------------------------------------------------------------------
def test_kill_tree_kills_a_child_and_its_children() -> None:
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(descendants(proc.pid)) < 1:
            time.sleep(0.1)
        tree = descendants(proc.pid)
        assert tree, "the grandchild never started"
        kill_tree(proc.pid)
        proc.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(pid_alive(p) for p in tree):
            time.sleep(0.1)
        assert not any(pid_alive(p) for p in tree)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert pid_alive(os.getpid())


# ---- queue adapters: conformance -------------------------------------------------------------
class _FakeCelery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.down = False

    def send_task(self, name, args, queue, priority):
        if self.down:
            raise OSError("connection refused")
        self.sent.append((args[0], queue.removeprefix("oran-jobs-")))

    def connection_for_write(self):
        app = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ensure_connection(self, max_retries):
                if app.down:
                    raise OSError("connection refused")

        return _Conn()


class _FakeRedis:
    def __init__(self) -> None:
        self.down = False
        self.jobs: list[tuple[str, str]] = []

    def ping(self):
        if self.down:
            raise OSError("connection refused")
        return True


def _fake_rq_queue(name, connection):
    class _Queue:
        def enqueue(self, fn, job_id, job_timeout):
            if connection.down:
                raise OSError("connection refused")
            assert fn == "oran_adapt.orchestrator.worker.run_job_by_id" and job_timeout > 0
            connection.jobs.append((job_id, name.removeprefix("oran-jobs-")))

    return _Queue()


def _kube(settings) -> tuple[KubernetesJobQueue, list[tuple[str, str]], dict[str, bool]]:
    created: dict[str, dict] = {}
    delivered: list[tuple[str, str]] = []
    state = {"down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("refused", request=request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        import json

        body = json.loads(request.content)
        name = body["metadata"]["name"]
        if name in created:
            return httpx.Response(409, json={"reason": "AlreadyExists"})
        created[name] = body
        delivered.append((body["metadata"]["annotations"]["oran.io/job-id"],
                          body["metadata"]["labels"]["oran.io/worker-class"]))
        return httpx.Response(201, json=body)

    api = HttpApi("http://kube.test", service="the Kubernetes API",
                  http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
                  token=StaticToken(None))
    k8s = settings.model_copy(update={"job_queue_k8s_image": "oran-adapt:test"})
    return KubernetesJobQueue(api, k8s), delivered, state


def _adapter(name: str, settings) -> tuple[JobQueuePort, conformance.Context]:
    if name == "database":
        return DatabaseQueue(), conformance.Context(delivered=None)
    if name == "inline":
        return InlineQueue(), conformance.Context(delivered=None)
    if name == "celery":
        app = _FakeCelery()
        return (CeleryQueue(app, settings, (OSError,)),
                conformance.Context(delivered=lambda: list(app.sent),
                                    break_broker=lambda: setattr(app, "down", True)))
    if name == "rq":
        redis = _FakeRedis()
        return (RqQueue(redis, _fake_rq_queue, settings, (OSError,)),
                conformance.Context(delivered=lambda: list(redis.jobs),
                                    break_broker=lambda: setattr(redis, "down", True)))
    queue, delivered, state = _kube(settings)
    return queue, conformance.Context(delivered=lambda: list(delivered),
                                      break_broker=lambda: state.update(down=True))


@pytest.mark.smoke
@pytest.mark.parametrize("name", ["database", "inline", "celery", "rq", "kubernetes"])
def test_job_queue_conformance(name, settings) -> None:
    port, ctx = _adapter(name, settings)
    assert conformance.run(port, ctx) == list(conformance.CHECKS)


@pytest.mark.smoke
def test_every_queue_adapter_is_installed_and_inline_is_development_only() -> None:
    installed = plugins.adapters("job_queue")
    assert set(installed) >= {"database", "inline", "celery", "rq", "kubernetes"}
    assert "development_only" in installed["inline"].capability.features
    assert "development_only" not in installed["database"].capability.features


@pytest.mark.smoke
@pytest.mark.parametrize(("name", "module"), [("celery", "celery"), ("rq", "rq")])
def test_missing_broker_sdk_names_the_extra(name, module, settings, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    configured = settings.model_copy(update={
        "job_queue_celery_broker_url": "redis://localhost:1/0",
        "job_queue_rq_redis_url": "redis://localhost:1/0",
    })
    with pytest.raises(ConfigurationError, match=f"oran-adapt\\[{name}\\]"):
        plugins.resolve("job_queue", name, config_key="job_queue_backend").factory(configured)


@pytest.mark.smoke
def test_kubernetes_job_manifest_per_class(settings) -> None:
    classed = settings.model_copy(update={"job_queue_k8s_class_pods": {"gpu": {
        "nodeSelector": {"accelerator": "nvidia"},
        "resources": {"limits": {"nvidia.com/gpu": 1}},
    }}})
    queue, _, _ = _kube(classed)
    manifest = queue.manifest(QueuedJob(job_id="ab" * 16, worker_class="gpu", tenant="t",
                                        priority=0, attempt=1))
    pod = manifest["spec"]["template"]["spec"]
    assert manifest["metadata"]["name"] == f"oran-job-{'ab' * 16}-a2"
    assert manifest["spec"]["backoffLimit"] == 0
    assert pod["nodeSelector"] == {"accelerator": "nvidia"}
    assert pod["containers"][0]["resources"] == {"limits": {"nvidia.com/gpu": 1}}
    assert pod["containers"][0]["command"][-2:] == ["--job-id", "ab" * 16]
