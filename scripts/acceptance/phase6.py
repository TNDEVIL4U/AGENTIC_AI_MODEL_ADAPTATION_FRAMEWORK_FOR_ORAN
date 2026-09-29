"""Hardening Phase 6 acceptance: the real execution layer, checked with real processes.

Run by scripts/verify.sh 6 after lint, the import-boundary test and the scoped tests.

1. A worker process killed mid-job: its lease runs out, the reaper requeues the job, a second
   worker runs it to exactly one outcome (attempt 2). Never stuck, never doubled.
2. A job past its deadline: the attempt's process and the process it started are actually
   dead, and the job is TIMED_OUT.
3. The same event_id sent at the same moment to two API replicas (two uvicorn processes, one
   database): one job; one answer 201, the other 200 naming the same job.
4. A cancel request on a running job (process mode): the attempt's process tree is dead within
   one checkpoint interval plus the kill grace, and the job is CANCELLED.
5. Vendor SDKs stay inside their adapters; every job queue adapter is documented in
   docs/adapters/job_queue.md and passes the conformance suite (celery, rq and kubernetes
   against doubles - unverified against real brokers and clusters).

Every process killed here is one this script (or its worker) started. Exit status 0 means
every check passed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 6 tests and the import-boundary check
sys.path.insert(0, str(HERE))  # phase6_pipelines, importable by job worker processes

# Short checkpoints so the gate is quick; the lease still exceeds two heartbeats.
FAST = {"job_heartbeat_s": 0.5, "job_lease_ttl_s": 2.0, "job_poll_interval_s": 0.2,
        "job_retry_backoff_s": 0.0, "job_kill_grace_s": 1.0}


def _values(tmp: Path, **overrides: object) -> dict[str, object]:
    return {
        "database_url": f"sqlite:///{(tmp / 'app.db').as_posix()}",
        "mlflow_tracking_uri": f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        "artifact_workdir": str(tmp / "work"),
        "log_json": False,
        "log_level": "WARNING",
        "auth_enabled": False,
        "notification_dispatch_enabled": False,
        "job_queue_backend": "database",
        **FAST,
        **overrides,
    }


def _settings(tmp: Path, **overrides: object):
    from oran_adapt.core.config import Settings
    from oran_adapt.db.migrate import upgrade_to_head

    settings = Settings(_env_file=None, **_values(tmp, **overrides))
    upgrade_to_head(settings.database_url)
    return settings


def _factory(settings):
    from oran_adapt.db.base import create_db_engine, make_session_factory

    return make_session_factory(create_db_engine(settings.database_url))


def _submit(factory, settings, model_id: str):
    from oran_adapt.adapters.job_queues import DatabaseQueue
    from oran_adapt.core.schemas import DriftEvent
    from oran_adapt.orchestrator.jobs import submit_adaptation_job

    return submit_adaptation_job(
        factory, DriftEvent(model_id=model_id, event_id=f"evt-{model_id}"), settings,
        registry=None, llm_client=None, workdir=settings.artifact_workdir, queue=DatabaseQueue(),
    )


def _job(factory, job_id: str):
    from sqlalchemy import select

    from oran_adapt.db.base import session_scope
    from oran_adapt.db.models import AdaptationEvent, AdaptationJob

    with session_scope(factory) as session:
        job = session.scalar(select(AdaptationJob).where(AdaptationJob.job_id == job_id))
        assert job is not None, f"job {job_id} is missing"
        path = list(session.scalars(select(AdaptationEvent.to_status)
                                    .where(AdaptationEvent.job_id == job_id)
                                    .order_by(AdaptationEvent.id)))
        return {"status": job.status, "attempt": job.attempt, "lost_count": job.lost_count,
                "lease_token": job.lease_token, "error": job.error, "path": path}


def _wait_for(predicate: Callable[[], object], timeout_s: float, what: str) -> object:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}")


def _pids(workdir: Path) -> list[int] | None:
    for found in workdir.rglob("pids"):
        text = found.read_text(encoding="utf-8").split()
        if len(text) == 2:
            return [int(p) for p in text]
    return None


def _cleanup(pids: list[int] | None) -> None:
    """Kill what the stand-in pipeline started, should a check fail before the framework
    did (its own children only)."""
    from oran_adapt.core.processes import kill_tree, pid_alive

    for pid in pids or []:
        if pid_alive(pid):
            kill_tree(pid)


_WORKER_PROCESS = """
import json, os
import phase6_pipelines
from oran_adapt.core.config import Settings
from oran_adapt.db.base import create_db_engine, make_session_factory
from oran_adapt.orchestrator import jobs
from oran_adapt.orchestrator.worker import Worker
jobs.run_adaptation_job = phase6_pipelines.tree_pipeline
settings = Settings(_env_file=None, **json.loads(os.environ["ORAN_TEST_SETTINGS"]))
factory = make_session_factory(create_db_engine(settings.database_url))
Worker(factory, settings, registry=None, llm_client=None, workdir=settings.artifact_workdir,
       owner="doomed-worker").run(max_jobs=1)
"""


def _env(values: dict[str, object], **extra: str) -> dict[str, str]:
    path = os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])
    return {**os.environ, "PYTHONPATH": path, "ORAN_TEST_SETTINGS": json.dumps(values), **extra}


def worker_killed_mid_job(tmp: Path) -> str:
    import phase6_pipelines

    from oran_adapt.adapters.job_queues import DatabaseQueue
    from oran_adapt.core.processes import kill_tree, pid_alive
    from oran_adapt.orchestrator import jobs
    from oran_adapt.orchestrator.worker import Worker, reap

    values = _values(tmp, job_execution_mode="thread")
    settings = _settings(tmp, job_execution_mode="thread")
    factory = _factory(settings)
    job_id = _submit(factory, settings, "m-killed").job_id
    proc = subprocess.Popen([sys.executable, "-c", _WORKER_PROCESS], env=_env(values),
                            cwd=str(tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = None
    try:
        def started() -> list[int] | None:
            assert proc.poll() is None, "the worker process exited before running the job"
            return _pids(tmp / "work")

        pids = _wait_for(started, 120, "the worker process to start the job")
        assert _job(factory, job_id)["status"] == "DATA_PREPARING"
    finally:
        kill_tree(proc.pid)  # the worker this check started, and its job's child
        proc.wait(timeout=30)
        _cleanup(pids)
    assert pids is not None and not any(pid_alive(p) for p in pids)

    killed = _job(factory, job_id)
    assert killed["status"] == "DATA_PREPARING", killed  # the dead worker recorded nothing
    assert reap(factory, settings, DatabaseQueue())["lease_expired"] == 0  # lease still valid
    time.sleep(settings.job_lease_ttl_s + 0.5)
    counts = reap(factory, settings, DatabaseQueue())
    assert counts["lease_expired"] == 1, counts
    requeued = _job(factory, job_id)
    assert requeued["status"] == "QUEUED" and requeued["lost_count"] == 1, requeued

    jobs.run_adaptation_job = phase6_pipelines.ok_pipeline
    worker = Worker(factory, settings, registry=None, llm_client=None,
                    workdir=settings.artifact_workdir, queue=DatabaseQueue())
    assert worker.run(once=True) == 1
    done = _job(factory, job_id)
    assert done["status"] == "COMPLETED" and done["attempt"] == 2, done
    assert done["path"].count("COMPLETED") == 1 and "FAILED" not in done["path"], done["path"]
    assert worker.run(once=True) == 0  # nothing left to run twice
    return (f"worker killed in DATA_PREPARING; lease expired after {settings.job_lease_ttl_s}s, "
            f"requeued (lost 1), completed once on attempt 2: {' -> '.join(done['path'])}")


def deadline_kills_the_process(tmp: Path) -> str:
    import phase6_pipelines

    from oran_adapt.adapters.job_queues import DatabaseQueue
    from oran_adapt.core.processes import pid_alive
    from oran_adapt.orchestrator import jobs
    from oran_adapt.orchestrator.worker import Worker, claim

    # A job worker process imports the whole stack (~18 s on the 16 GB dev laptop): the
    # deadline leaves it time to start the work.
    deadline_s = 30.0
    settings = _settings(tmp, job_execution_mode="process", job_deadline_s=deadline_s)
    factory = _factory(settings)
    jobs.run_adaptation_job = phase6_pipelines.tree_pipeline
    job_id = _submit(factory, settings, "m-deadline").job_id
    started = time.monotonic()
    got = claim(factory, settings, owner="gate-worker")
    assert got is not None and got.job_id == job_id
    pids = None
    try:
        Worker(factory, settings, registry=None, llm_client=None,
               workdir=settings.artifact_workdir, queue=DatabaseQueue()).run_claim(got)
        elapsed = time.monotonic() - started
        pids = _pids(tmp / "work")
        assert pids is not None, "the job never started before its deadline"
        job = _job(factory, job_id)
        assert job["status"] == "TIMED_OUT", job
        assert elapsed < deadline_s + settings.job_kill_grace_s + 10, elapsed
        _wait_for(lambda: not any(pid_alive(p) for p in pids), 10,
                  "the job's process tree to die")
        assert not any(Path(p).exists() for p in (tmp / "work").rglob("finished"))
    finally:
        _cleanup(pids)
    return (f"deadline {deadline_s:.0f}s: TIMED_OUT after {elapsed:.1f}s; worker pid {pids[0]} "
            f"and its child {pids[1]} are dead")


_API_PROCESS = """
import json, os, uvicorn
from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
settings = Settings(_env_file=None, **json.loads(os.environ["ORAN_TEST_SETTINGS"]))
uvicorn.run(create_app(settings), host="127.0.0.1", port=int(os.environ["ORAN_TEST_PORT"]),
            log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def duplicate_event_two_replicas(tmp: Path) -> str:
    import httpx
    from mlflow.tracking import MlflowClient
    from sqlalchemy import func, select

    from oran_adapt.db.base import session_scope
    from oran_adapt.db.models import AdaptationJob

    settings = _settings(tmp)
    values = _values(tmp)
    # Create the gate's local MLflow store once: two replicas creating its schema at the same
    # moment would race (a real deployment points both at one MLflow server).
    MlflowClient(tracking_uri=settings.mlflow_tracking_uri).search_experiments()
    ports = [_free_port(), _free_port()]
    logs = [(tmp / f"replica-{i}.log").open("wb") for i in range(len(ports))]
    replicas = [subprocess.Popen([sys.executable, "-c", _API_PROCESS], cwd=str(tmp),
                                 env=_env(values, ORAN_TEST_PORT=str(port)),
                                 stdout=subprocess.DEVNULL, stderr=log)
                for port, log in zip(ports, logs, strict=True)]
    try:
        def up(port: int, proc: subprocess.Popen) -> bool:
            if proc.poll() is not None:
                i = replicas.index(proc)
                logs[i].flush()
                tail = (tmp / f"replica-{i}.log").read_text(errors="replace")[-600:]
                raise AssertionError(f"an API replica exited during startup: {tail}")
            try:
                return httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=2)\
                    .status_code == 200
            except httpx.TransportError:
                return False

        for port, proc in zip(ports, replicas, strict=True):
            _wait_for(lambda port=port, proc=proc: up(port, proc), 120, "the API replicas")
        barrier = threading.Barrier(2)
        answers: list[tuple[int, dict]] = []
        event = {"model_id": "m-replicas", "event_id": "evt-replicas-1"}

        def send(port: int) -> None:
            barrier.wait()
            r = httpx.post(f"http://127.0.0.1:{port}/api/v1/adaptation/events", json=event,
                           timeout=60)
            answers.append((r.status_code, r.json()))

        senders = [threading.Thread(target=send, args=(port,)) for port in ports]
        for t in senders:
            t.start()
        for t in senders:
            t.join(90)
    finally:
        for proc in replicas:
            proc.kill()  # the replicas this check started, and only them
            proc.wait(timeout=30)
        for log in logs:
            log.close()
    assert len(answers) == 2, answers
    assert sorted(code for code, _ in answers) == [200, 201], answers
    assert answers[0][1]["job_id"] == answers[1][1]["job_id"], answers
    with session_scope(_factory(settings)) as session:
        count = session.scalar(select(func.count()).select_from(AdaptationJob))
    assert count == 1, f"{count} jobs for one event"
    return f"two uvicorn replicas, one event_id at once: 201 + 200, one job ({answers[0][1]['status']})"


def cancel_within_checkpoint(tmp: Path) -> str:
    import phase6_pipelines

    from oran_adapt.adapters.job_queues import DatabaseQueue
    from oran_adapt.core.processes import pid_alive
    from oran_adapt.orchestrator import jobs
    from oran_adapt.orchestrator.jobs import request_cancel
    from oran_adapt.orchestrator.worker import Worker, claim

    settings = _settings(tmp, job_execution_mode="process")
    factory = _factory(settings)
    jobs.run_adaptation_job = phase6_pipelines.tree_pipeline
    job_id = _submit(factory, settings, "m-cancel").job_id
    got = claim(factory, settings, owner="gate-worker")
    assert got is not None
    worker = Worker(factory, settings, registry=None, llm_client=None,
                    workdir=settings.artifact_workdir, queue=DatabaseQueue())
    runner = threading.Thread(target=worker.run_claim, args=(got,))
    runner.start()
    pids = None
    try:
        pids = _wait_for(lambda: _pids(tmp / "work"), 120, "the job to start working")
        requested = time.monotonic()
        _, immediate = request_cancel(factory, settings, job_id, actor="gate")
        assert not immediate, "a running job was cancelled without its worker"
        _wait_for(lambda: not any(pid_alive(p) for p in pids), 30, "the job's processes to die")
        stopped = time.monotonic() - requested
        runner.join(30)
        assert not runner.is_alive(), "the worker did not return after the cancel"
    finally:
        _cleanup(pids)
    bound = settings.job_heartbeat_s + settings.job_kill_grace_s + 2.0
    assert stopped < bound, f"work stopped {stopped:.1f}s after the cancel (bound {bound}s)"
    job = _job(factory, job_id)
    assert job["status"] == "CANCELLED" and job["error"]["code"] == "JOB_CANCELLED", job
    return (f"cancel of a running job: process tree dead {stopped:.1f}s later (checkpoint "
            f"{settings.job_heartbeat_s}s + grace {settings.job_kill_grace_s}s); CANCELLED")


def boundaries_docs_conformance(tmp: Path) -> str:
    import pytest
    from test_import_boundary import violations

    from oran_adapt import plugins

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    guide = (ROOT / "docs" / "adapters" / "job_queue.md").read_text(encoding="utf-8")
    undocumented = [a for a in plugins.adapters("job_queue") if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/job_queue.md: {undocumented}"
    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore",
                        f"{TESTS / 'test_phase6_execution.py'}::test_job_queue_conformance"])
    assert code == 0, "the job queue conformance suite failed"
    return ("import boundary clean; all job queue adapters documented and conformant "
            "(celery, rq, kubernetes against doubles: unverified against real services)")


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("worker killed mid-job: requeued, finished once", worker_killed_mid_job),
    ("deadline exceeded: the process is actually terminated", deadline_kills_the_process),
    ("duplicate event_id across two API replicas: one job", duplicate_event_two_replicas),
    ("cancel stops work within the checkpoint interval", cancel_within_checkpoint),
    ("boundaries, documentation and conformance", boundaries_docs_conformance),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase6-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 6 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
