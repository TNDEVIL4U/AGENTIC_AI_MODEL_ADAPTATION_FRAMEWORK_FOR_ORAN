"""The running job, as seen from inside the pipeline. The job wrapper sets it in the worker
thread before calling the pipeline, so the pipeline can report each stage it enters (recorded
as a state transition) and tag what it writes with the job id, without the pipeline's signature
changing. Called directly, outside the wrapper (as the Phase 9 tests do), nothing is set and
stage reports are no-ops.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from oran_adapt.core.enums import JobStatus


@dataclass(frozen=True)
class JobContext:
    job_id: str
    on_stage: Callable[[JobStatus, str], None]


current_job: ContextVar[JobContext | None] = ContextVar("current_job", default=None)


def current_job_id() -> str | None:
    ctx = current_job.get()
    return ctx.job_id if ctx else None


def report_stage(status: JobStatus, message: str = "") -> None:
    ctx = current_job.get()
    if ctx is not None:
        ctx.on_stage(status, message)
