"""JobExecutorPort adapters ``process`` (default) and ``thread``.

Both run ``call.run(call.payload)`` bounded by ``call.timeout_s`` and raise JobTimeoutError when it
runs over. ``run`` returns a JSON-safe dict or raises; an AdaptationError keeps its class across
the process boundary (core.errors.rebuild_error), so retry decisions are the same in both modes.

Checkpoints: with ``call.tick_s`` set, the executor wakes every ``tick_s`` seconds while the
attempt runs and calls ``call.on_tick()`` (the worker's supervisor: lease renewal, cancel
requests, deadline, drain). When that returns an exception, the attempt is stopped as on a
timeout and the exception raised.

``process``: a fresh ``spawn`` worker per call; ``run`` and ``payload`` must pickle. On a timeout
or a stop the worker's whole process tree is terminated (core.processes): on POSIX its process
group, which it leads, and on every platform each descendant found by walking the process table,
so the sandbox subprocess and the real interpreter behind a Windows venv launcher die too. The
worker watches a pipe from this process and exits, killing its own subprocesses, when this
process dies, so killing a job worker never leaves the attempt running. A worker that exits
without reporting raises JobWorkerLostError. Metric movements inside the worker are forwarded
to this process.

``thread``: a worker thread of this process, for tests and debugging with in-process fakes.
Python cannot kill a thread, so on a timeout or a stop it is left to finish and
``call.on_abandoned`` runs when it does; the lease fence refuses whatever it would still record.
"""

from __future__ import annotations

import contextvars
import json
import multiprocessing
import os
import pickle  # nosec B403 - only to check that job inputs can reach the worker
import signal
import sys
import threading
import time
from collections.abc import Callable
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
    JobWorkerLostError,
    rebuild_error,
)
from oran_adapt.core.processes import descendants, terminate
from oran_adapt.ports import AdapterSpec, Capability, JobCall

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


def _timeout(call: JobCall, **context: Any) -> JobTimeoutError:
    return JobTimeoutError(
        f"adaptation job exceeded its {call.timeout_s:.0f}s timeout",
        timeout_s=call.timeout_s,
        **context,
    )


def _supervise(call: JobCall, wait: Callable[[float], bool]) -> Exception | None:
    """Wait for the attempt, in ``call.tick_s`` slices when set, calling ``call.on_tick`` after
    each. ``wait(seconds)`` returns True once the attempt has finished. Returns None when it
    finished, else the exception to stop it with: a JobTimeoutError past ``call.timeout_s``, or
    whatever ``on_tick`` returned."""
    deadline = time.monotonic() + call.timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _timeout(call)
        if wait(remaining if call.tick_s is None else min(remaining, call.tick_s)):
            return None
        if time.monotonic() >= deadline:
            return _timeout(call)
        if call.on_tick is not None:
            stop = call.on_tick()
            if stop is not None:
                return stop


class ThreadJobExecutor:
    in_process = True

    def execute(self, call: JobCall) -> dict[str, Any]:
        # The worker runs in a copy of this context, so the caller's correlation id reaches it.
        ctx = contextvars.copy_context()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(ctx.run, call.run, call.payload)

        def _wait(seconds: float) -> bool:
            try:
                future.exception(timeout=seconds)
            except FutureTimeoutError:
                return False
            return True

        try:
            stop = _supervise(call, _wait)
            if stop is None:
                result: dict[str, Any] = future.result()
                return result
            on_abandoned = call.on_abandoned
            if on_abandoned is not None:
                future.add_done_callback(lambda _f: on_abandoned())
            raise stop
        finally:
            pool.shutdown(wait=False)


def _plain(context: dict[str, Any]) -> dict[str, Any]:
    """``context`` reduced to JSON-safe values, so it always survives the trip to the parent."""
    result: dict[str, Any] = json.loads(json.dumps(context, default=str))
    return result


def _exit_with_parent(watch: Connection) -> None:
    """Block on the pipe the parent never writes to. It closes when the parent dies: this
    worker then kills its own subprocesses and exits, so the attempt cannot outlive it."""
    try:
        watch.recv()
    except (EOFError, OSError):
        pass
    for pid in reversed(descendants(os.getpid())):
        terminate(pid)
    os._exit(1)


def _worker_main(conn: Connection, watch: Connection, run: Any, payload: dict[str, Any]) -> None:
    """Sends back exactly one message: ("ok", deltas, result) | ("error", deltas, code, message,
    context) | ("crash", deltas, text), deltas being how far this worker moved the forwarded
    metrics (metrics.delta_since)."""
    if sys.platform != "win32":
        os.setpgrp()  # lead a process group, so a timeout kills subprocesses too
    threading.Thread(target=_exit_with_parent, args=(watch,), daemon=True,
                     name="oran-parent-watch").start()
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
        """Stop the worker and everything below it: the processes it started are killed (on
        POSIX its process group is sent SIGTERM first), then the worker is terminated and
        killed if it is still alive after the grace period. Only ever touches the process this
        executor started and its descendants."""
        pid = proc.pid
        if pid is not None:
            for child in reversed(descendants(pid)):
                terminate(child)
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
        watch_end, alive_end = mp.Pipe(duplex=False)
        proc = mp.Process(
            target=_worker_main,
            args=(send_end, watch_end, call.run, call.payload),
            name=f"oran-job-{call.job_id[:8]}",
        )
        proc.start()
        send_end.close()  # only the child writes; EOF then means the child is gone
        watch_end.close()  # only the child reads; alive_end closing tells it we are gone
        message = None
        try:
            stop = _supervise(call, recv_end.poll)
            if stop is not None:
                self._stop(proc)
                if isinstance(stop, JobTimeoutError):
                    stop.context["worker_pid"] = proc.pid
                raise stop
            try:
                message = recv_end.recv()
            except EOFError:
                message = None
        finally:
            recv_end.close()
            proc.join(self.kill_grace_s)
            if proc.is_alive():
                self._stop(proc)
            alive_end.close()

        if message is None:
            raise JobWorkerLostError(
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
