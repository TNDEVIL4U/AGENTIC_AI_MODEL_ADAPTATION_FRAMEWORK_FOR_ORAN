"""What each LLM call did, for the job record.

``ask`` is the one way the pipeline talks to an LLM: it sends a versioned prompt, and appends
an LlmCall (prompt id, version and hash, outcome, fallback reason, tokens, cost, duration) to the
calls being recorded for the current job (``recording``). The job's result carries them as
``llm_calls``.

Provider adapters that know the real token counts report them with ``report_usage``; the guard
(llm.guard) reads them back with ``take_usage`` and otherwise estimates from the text length.
Both use context variables, so concurrent jobs in threads never see each other's calls.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from oran_adapt.core import metrics
from oran_adapt.core.errors import LlmUnavailableError

if TYPE_CHECKING:
    from oran_adapt.llm.prompts import Prompt
    from oran_adapt.ports import LLMPort

# Fallback reasons: why the deterministic rules decided instead of the LLM.
FALLBACK_DISABLED = "disabled"
FALLBACK_INVALID_OUTPUT = "invalid_output"
FALLBACK_OUTSIDE_SET = "outside_compatible_set"
FALLBACK_UNSAFE_CODE = "unsafe_code"
FALLBACK_SANDBOX_FAILED = "sandbox_failed"


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    # True when the provider did not report the counts and they were estimated from text.
    estimated: bool = False
    cost: float = 0.0


@dataclass
class LlmCall:
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    # "ok", or the fallback reason (circuit_open, budget_exceeded, unavailable, ...).
    outcome: str = "ok"
    error_code: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_tokens: bool = False
    cost: float = 0.0
    duration_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_calls: ContextVar[list[LlmCall] | None] = ContextVar("oran_llm_calls", default=None)
_usage: ContextVar[Usage | None] = ContextVar("oran_llm_usage", default=None)
_provider: ContextVar[str | None] = ContextVar("oran_llm_provider", default=None)
_job: ContextVar[str | None] = ContextVar("oran_llm_job", default=None)
_prompt: ContextVar[dict[str, str] | None] = ContextVar("oran_llm_prompt", default=None)


@contextmanager
def recording(job_id: str | None = None) -> Iterator[list[LlmCall]]:
    """Collect every LlmCall made inside the block (nested blocks collect their own). The
    usage ledger files the calls under ``job_id``."""
    calls: list[LlmCall] = []
    token, job_token = _calls.set(calls), _job.set(job_id)
    try:
        yield calls
    finally:
        _job.reset(job_token)
        _calls.reset(token)


def current_job() -> str | None:
    """The job the calls being made belong to, if a recording names one."""
    return _job.get()


def current_prompt() -> dict[str, str]:
    """The stamp (id, version, hash) of the prompt being sent, empty outside ``ask``."""
    return dict(_prompt.get() or {})


def report_usage(input_tokens: int, output_tokens: int) -> None:
    """For provider adapters: the token counts the provider reported for the call just made."""
    _usage.set(Usage(max(0, int(input_tokens)), max(0, int(output_tokens))))


def take_usage() -> Usage | None:
    """The usage reported since the last take, and forget it."""
    usage = _usage.get()
    _usage.set(None)
    return usage


def note_call(usage: Usage, provider: str) -> None:
    """For the guard: the settled usage of the call in progress."""
    _usage.set(usage)
    _provider.set(provider)


def fallback(reason: str) -> None:
    """Count a decision or adaptation that fell back to the deterministic rules."""
    metrics.LLM_FALLBACKS.labels(reason).inc()


def ask(client: LLMPort, prompt: Prompt, user: str, **extra: Any) -> str:
    """Send ``prompt`` with ``user`` as the user turn and record the call. Raises
    LlmUnavailableError (or a subclass) exactly as the client does."""
    call = LlmCall(prompt.id, prompt.version, prompt.sha256, extra=dict(extra))
    _usage.set(None)
    _provider.set(None)
    prompt_token = _prompt.set(prompt.stamp())
    started = time.monotonic()
    try:
        return client.complete(system=prompt.system, prompt=user)
    except LlmUnavailableError as exc:
        call.outcome = exc.reason
        call.error_code = exc.code
        raise
    except Exception:  # recorded, then re-raised unchanged
        call.outcome = "error"
        raise
    finally:
        _prompt.reset(prompt_token)
        call.duration_ms = int((time.monotonic() - started) * 1000)
        usage = _usage.get()
        if usage is not None:
            call.input_tokens, call.output_tokens = usage.input_tokens, usage.output_tokens
            call.estimated_tokens, call.cost = usage.estimated, usage.cost
        call.provider = _provider.get()
        recorded = _calls.get()
        if recorded is not None:
            recorded.append(call)
