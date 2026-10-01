"""The adaptation job state machine: which status may follow which. Every persisted transition
goes through check_transition, so a job can never jump, say, from RECEIVED straight to
PROMOTING, or leave a terminal state."""

from __future__ import annotations

from oran_adapt.core.enums import TERMINAL_STATUSES, JobStatus
from oran_adapt.core.errors import InvalidTransitionError

S = JobStatus

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    # RECEIVED -> VALIDATING directly only for a row an older release left RECEIVED.
    S.RECEIVED: frozenset({S.QUEUED, S.VALIDATING, S.FAILED}),
    # A worker claimed it; FAILED when quarantined or out of retries while waiting.
    S.QUEUED: frozenset({S.VALIDATING, S.FAILED}),
    S.VALIDATING: frozenset({S.DATA_PREPARING, S.FAILED}),
    # COMPLETED straight from data preparation: no drift confirmed, or not enough data.
    S.DATA_PREPARING: frozenset({S.EVALUATING_VERSIONS, S.DECISION_PENDING, S.COMPLETED, S.FAILED}),
    S.EVALUATING_VERSIONS: frozenset({S.REUSE_DECISION, S.FAILED}),
    S.REUSE_DECISION: frozenset({S.PROMOTING, S.DECISION_PENDING, S.FAILED}),
    S.DECISION_PENDING: frozenset({S.ADAPTING, S.COMPLETED, S.FAILED}),
    S.ADAPTING: frozenset({S.VALIDATING_CANDIDATE, S.FAILED}),
    # COMPLETED from candidate validation: the candidate was rejected and LIVE never moved.
    S.VALIDATING_CANDIDATE: frozenset({S.REGISTERING, S.COMPLETED, S.FAILED}),
    S.REGISTERING: frozenset({S.PROMOTING, S.FAILED}),
    S.PROMOTING: frozenset({S.COMPLETED, S.ROLLED_BACK, S.FAILED}),
}

# A transient failure (MLflow / database unreachable, a worker that died or lost its lease)
# puts the job back in the queue from wherever it had got to; the next attempt starts over.
_RETRY_TARGET = S.QUEUED


def allowed_next(status: JobStatus) -> frozenset[JobStatus]:
    if status in TERMINAL_STATUSES:
        return frozenset()
    nxt = set(_ALLOWED.get(status, frozenset({S.FAILED})))
    # A deadline and a cancel request can strike at any stage that has not ended.
    nxt.update({S.TIMED_OUT, S.CANCELLED})
    if status not in (S.RECEIVED, S.QUEUED):
        nxt.add(_RETRY_TARGET)
    return frozenset(nxt)


def check_transition(current: str | JobStatus, target: str | JobStatus) -> None:
    """Raise InvalidTransitionError unless ``current -> target`` is allowed."""
    cur, tgt = JobStatus(current), JobStatus(target)
    if tgt not in allowed_next(cur):
        raise InvalidTransitionError(
            f"job cannot move from {cur} to {tgt}", from_status=cur, to_status=tgt
        )
