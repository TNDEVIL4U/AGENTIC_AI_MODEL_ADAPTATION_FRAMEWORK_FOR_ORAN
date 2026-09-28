"""JobExecutorPort adapters ``process`` (default) and ``thread``.

Both run ``call.run(call.payload)`` bounded by ``call.timeout_s`` and raise JobTimeoutError when it
runs over. ``run`` returns a JSON-safe dict or raises; an AdaptationError keeps its class across
the process boundary (core.errors.rebuild_error), so retry decisions are the same in both modes.

``process``: a fresh ``spawn`` worker per call; ``run`` and ``payload`` must pickle. On a timeout
the worker is terminated, then killed after JOB_KILL_GRACE_S. On POSIX the worker leads its own
process group, so subprocesses it started (the sandbox) die with it; on Windows only the worker
itself is killed. Metric movements inside the worker are forwarded to this process.

``thread``: a worker thread of this process, for tests and debugging with in-process fakes.
Python cannot kill a thread, so on a timeout it is left to finish and ``call.on_abandoned`` runs
when it does.
"""

from __future__ import annotations

import contextvars
import json
import multiprocessing
import os
import pickle  # nosec B403 - only to check that job inputs can reach the worker
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING, Any

from oran_adapt.core import metrics
from oran_adapt.core.errors import (
    AdaptationError,
    ConfigurationError,
    JobTimeoutError,
    JobWorkerError,
    rebuild_error,
)
from oran_adapt.ports import AdapterSpec, Capability, JobCall

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def _timeout(call: JobCall, **context: Any) -> JobTimeoutError:
    return JobTimeoutError(
        f"adaptation job exceeded its {call.timeout_s:.0f}s timeout",
        timeout_s=call.timeout_s,
        **context,
    )


class ThreadJobExecutor:
    in_process = True

    def execute(self, call: JobCall) -> dict[str, Any]:
        # The worker runs in a copy of this context, so the caller's correlation id reaches it.
        ctx = contextvars.copy_context()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(ctx.run, call.run, call.payload)
        try:
            return future.result(timeout=call.timeout_s)
        except FutureTimeoutError as exc:
            on_abandoned = call.on_abandoned
            if on_abandoned is not None:
                future.add_done_callback(lambda _f: on_abandoned())
            raise _timeout(call) from exc
        finally:
            pool.shutdown(wait=False)


def _plain(context: dict[str, Any]) -> dict[str, Any]:
    """``context`` reduced to JSON-safe values, so it always survives the trip to the parent."""
    result: dict[str, Any] = json.loads(json.dumps(context, default=str))
    return result


def _worker_main(conn: Connection, run: Any, payload: dict[str, Any]) -> None:
    """Sends back exactly one message: ("ok", deltas, result) | ("error", deltas, code, message,
    context) | ("crash", deltas, text), deltas being how far this worker moved the forwarded
    metrics (metrics.delta_since)."""
    if sys.platform != "win32":
        os.setpgrp()  # lead a process group, so a timeout kills subprocesses too
    before = metrics.snapshot()
    try:
        result = run(payload)
        conn.send(("ok", metrics.delta_since(before), result))
    except AdaptationError as exc:
        conn.send(("error", metrics.delta_since(before), exc.code, exc.message, _plain(exc.context)))
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which records it
        conn.send(("crash", metrics.delta_since(before), f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


class ProcessJobExecutor:
    in_process = False

    def __init__(self, kill_grace_s: float) -> None:
        self.kill_grace_s = kill_grace_s

    def _stop(self, proc: multiprocessing.process.BaseProcess) -> None:
        """Terminate the worker (on POSIX its process group), then kill it if it is still alive
        after the grace period. Only ever touches the process this executor started."""
        pid = proc.pid
        if sys.platform != "win32" and pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()  # the worker had not become a group leader yet
        else:
            proc.terminate()
        proc.join(self.kill_grace_s)
        if proc.is_alive():
            if sys.platform != "win32" and pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            proc.kill()
            proc.join(self.kill_grace_s)

    def execute(self, call: JobCall) -> dict[str, Any]:
        try:
            pickle.dumps((call.run, call.payload))
        except Exception as exc:
            raise ConfigurationError(
                "job inputs cannot be sent to a worker process",
                key="JOB_EXECUTION_MODE",
                cause=str(exc),
                hint="in-process fakes need JOB_EXECUTION_MODE=thread",
            ) from exc

        mp = multiprocessing.get_context("spawn")
        recv_end, send_end = mp.Pipe(duplex=False)
        proc = mp.Process(
            target=_worker_main,
            args=(send_end, call.run, call.payload),
            name=f"oran-job-{call.job_id[:8]}",
        )
        proc.start()
        send_end.close()  # only the child writes; EOF then means the child is gone
        message = None
        try:
            if not recv_end.poll(call.timeout_s):
                self._stop(proc)
                raise _timeout(call, worker_pid=proc.pid)
            try:
                message = recv_end.recv()
            except EOFError:
                message = None
        finally:
            recv_end.close()
            proc.join(self.kill_grace_s)
            if proc.is_alive():
                self._stop(proc)

        if message is None:
            raise JobWorkerError(
                f"job worker process exited with code {proc.exitcode} and no result",
                exitcode=proc.exitcode,
            )
        kind, deltas, *rest = message
        metrics.apply_delta(deltas)
        if kind == "ok":
            ok: dict[str, Any] = rest[0]
            return ok
        if kind == "error":
            raise rebuild_error(*rest)
        raise JobWorkerError(rest[0])


def _process(settings: Settings) -> ProcessJobExecutor:
    return ProcessJobExecutor(settings.job_kill_grace_s)


def _thread(settings: Settings) -> ThreadJobExecutor:
    return ThreadJobExecutor()


PROCESS = AdapterSpec(
    capability=Capability(
        port="job_executor",
        adapter="process",
        description="one spawned worker process per job, killed on timeout",
        features=frozenset({"hard_timeout", "isolated"}),
        config_keys=("job_timeout_s", "job_kill_grace_s"),
    ),
    factory=_process,
)

THREAD = AdapterSpec(
    capability=Capability(
        port="job_executor",
        adapter="thread",
        description="a worker thread of this process; soft timeout (tests, debugging)",
        features=frozenset({"in_process"}),
        config_keys=("job_timeout_s",),
    ),
    factory=_thread,
)
