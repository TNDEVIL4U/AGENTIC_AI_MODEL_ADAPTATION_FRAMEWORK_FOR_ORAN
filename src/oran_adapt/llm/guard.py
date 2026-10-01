"""The guard every LLM call goes through: size and budget caps, retries, a circuit breaker and the
usage ledger.

Before a call is sent:
  * its input is estimated (``LLM_CHARS_PER_TOKEN``) and refused above ``LLM_MAX_INPUT_TOKENS``;
  * the usage recorded in the last ``LLM_BUDGET_WINDOW_S`` plus this call's worst case (its input
    and ``LLM_MAX_OUTPUT_TOKENS``) must stay within ``LLM_TOKEN_BUDGET`` and ``LLM_COST_BUDGET``;
  * the provider's circuit must be closed, or half-open with no trial already in flight.
A refusal raises LlmBudgetExceededError or LlmCircuitOpenError, both LlmUnavailableError, so the
caller falls back to the deterministic rules exactly as for an unreachable provider.

A failed call is retried ``LLM_MAX_RETRIES`` times with exponential backoff (the provider SDKs
never retry on their own). ``LLM_BREAKER_FAILURE_THRESHOLD`` consecutive failed calls open the
circuit for ``LLM_BREAKER_RESET_S``; then one trial call is let through, and its outcome closes or
reopens the circuit. Circuits are per provider and per process.

After a call the tokens the provider reported (or, failing that, estimates) are priced with
``LLM_COST_PER_1K_*_TOKENS`` and written to the usage ledger (the llm_usage table).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from oran_adapt.core import metrics
from oran_adapt.core.errors import (
    LlmBudgetExceededError,
    LlmCircuitOpenError,
    LlmUnavailableError,
)
from oran_adapt.llm import calls

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import LLMPort

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardPolicy:
    max_retries: int = 0
    retry_backoff_s: float = 0.5
    breaker_failure_threshold: int = 3
    breaker_reset_s: float = 60.0
    max_input_tokens: int = 0
    max_output_tokens: int = 2048
    token_budget: int = 0
    cost_budget: float = 0.0
    budget_window_s: int = 86400
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    chars_per_token: float = 4.0

    @classmethod
    def from_settings(cls, settings: Settings) -> GuardPolicy:
        return cls(
            max_retries=settings.llm_max_retries,
            retry_backoff_s=settings.llm_retry_backoff_s,
            breaker_failure_threshold=settings.llm_breaker_failure_threshold,
            breaker_reset_s=settings.llm_breaker_reset_s,
            max_input_tokens=settings.llm_max_input_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
            token_budget=settings.llm_token_budget,
            cost_budget=settings.llm_cost_budget,
            budget_window_s=settings.llm_budget_window_s,
            cost_per_1k_input=settings.llm_cost_per_1k_input_tokens,
            cost_per_1k_output=settings.llm_cost_per_1k_output_tokens,
            chars_per_token=settings.llm_chars_per_token,
        )

    def tokens(self, text: str) -> int:
        return int(len(text) / self.chars_per_token) + 1 if text else 0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.cost_per_1k_input
                + output_tokens * self.cost_per_1k_output) / 1000


# ---- usage ledger ------------------------------------------------------------------------------
class UsageLedger(Protocol):
    def spent(self, since: datetime) -> tuple[int, float]:
        """(tokens, cost) recorded since ``since``, across providers."""
        ...

    def record(self, provider: str, usage: calls.Usage, outcome: str) -> None:
        ...


class MemoryUsageLedger:
    """A per-process ledger, for tests and for runs without a database."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.rows: list[tuple[datetime, str, int, float, str]] = []
        self._clock = clock or (lambda: datetime.now(UTC))

    def spent(self, since: datetime) -> tuple[int, float]:
        rows = [r for r in self.rows if r[0] >= since]
        return sum(r[2] for r in rows), sum(r[3] for r in rows)

    def record(self, provider: str, usage: calls.Usage, outcome: str) -> None:
        self.rows.append((self._clock(), provider,
                          usage.input_tokens + usage.output_tokens, usage.cost, outcome))


class SqlUsageLedger:
    """The llm_usage table of DATABASE_URL, so every API process and worker shares one budget.
    The engine is created on first use and dropped when pickled."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._engine: Engine | None = None

    def __getstate__(self) -> dict[str, Any]:
        return {"database_url": self.database_url}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(state["database_url"])  # type: ignore[misc]

    def _get_engine(self) -> Engine:
        if self._engine is None:
            from oran_adapt.db.base import create_db_engine

            self._engine = create_db_engine(self.database_url)
        return self._engine

    def spent(self, since: datetime) -> tuple[int, float]:
        from sqlalchemy import func, select
        from sqlalchemy.exc import SQLAlchemyError

        from oran_adapt.db.models import LlmUsage

        query = select(
            func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0),
            func.coalesce(func.sum(LlmUsage.cost), 0.0),
        ).where(LlmUsage.created_at >= since)
        try:
            with self._get_engine().connect() as conn:
                tokens, cost = conn.execute(query).one()
        except SQLAlchemyError as exc:
            # A budget that cannot be checked is not spent: refuse rather than overspend.
            raise LlmBudgetExceededError(
                "the LLM usage ledger cannot be read, so the budget cannot be checked",
                cause=type(exc).__name__,
            ) from exc
        return int(tokens), float(cost)

    def record(self, provider: str, usage: calls.Usage, outcome: str) -> None:
        from sqlalchemy.exc import SQLAlchemyError
        from sqlalchemy.orm import Session

        from oran_adapt.db.models import LlmUsage

        stamp = calls.current_prompt()
        row = LlmUsage(
            provider=provider,
            prompt_id=stamp.get("prompt_id"),
            prompt_version=stamp.get("prompt_version"),
            job_id=calls.current_job(),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated=usage.estimated,
            cost=usage.cost,
            outcome=outcome,
        )
        try:
            with Session(self._get_engine()) as session:
                session.add(row)
                session.commit()
        except SQLAlchemyError as exc:
            # The call already happened; losing its ledger row must not fail the job.
            log.warning("llm usage not recorded", extra={"provider": provider,
                                                         "cause": type(exc).__name__})


# ---- circuit breaker ---------------------------------------------------------------------------
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, provider: str, threshold: int, reset_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.provider = provider
        self.threshold = threshold
        self.reset_s = reset_s
        self.clock = clock
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Raise LlmCircuitOpenError unless a call may go through now."""
        with self._lock:
            if self.state == self.CLOSED:
                return
            if self.state == self.OPEN and self.clock() - self.opened_at >= self.reset_s:
                self.state = self.HALF_OPEN  # this caller makes the one trial call
                return
            retry_in = max(0.0, self.reset_s - (self.clock() - self.opened_at))
        raise LlmCircuitOpenError(
            "the LLM circuit is open after repeated failures", provider=self.provider,
            retry_in_s=round(retry_in, 1),
        )

    def success(self) -> None:
        with self._lock:
            self.state, self.failures = self.CLOSED, 0
        metrics.LLM_CIRCUIT_OPEN.labels(self.provider).set(0)

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                self.state, self.opened_at = self.OPEN, self.clock()
            opened = self.state == self.OPEN
        if opened:
            metrics.LLM_CIRCUIT_OPEN.labels(self.provider).set(1)


_breakers: dict[tuple[str, int, float], CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker_for(provider: str, policy: GuardPolicy) -> CircuitBreaker:
    """The process-wide breaker of ``provider`` (shared by every client built for it)."""
    key = (provider, policy.breaker_failure_threshold, policy.breaker_reset_s)
    with _breakers_lock:
        if key not in _breakers:
            _breakers[key] = CircuitBreaker(provider, *key[1:])
        return _breakers[key]


def reset_breakers() -> None:
    """Forget every breaker's state (tests, and an operator reloading the configuration)."""
    with _breakers_lock:
        _breakers.clear()


# ---- the guard ---------------------------------------------------------------------------------
class GuardedLlmClient:
    """LLMPort around ``inner``: see the module docstring for what it enforces."""

    def __init__(
        self,
        inner: LLMPort,
        provider: str,
        policy: GuardPolicy,
        ledger: UsageLedger | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.inner = inner
        self.provider = provider
        self.policy = policy
        self.ledger = ledger
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(cls, inner: LLMPort, provider: str, settings: Settings) -> GuardedLlmClient:
        return cls(inner, provider, GuardPolicy.from_settings(settings),
                   SqlUsageLedger(settings.database_url))

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state.pop("_sleep", None)
        state.pop("_now", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._sleep = time.sleep
        self._now = lambda: datetime.now(UTC)

    def _refuse(self, reason: str, message: str, **context: Any) -> LlmBudgetExceededError:
        metrics.LLM_REFUSED.labels(self.provider, reason).inc()
        return LlmBudgetExceededError(message, provider=self.provider, cap=reason, **context)

    def _check_budget(self, input_tokens: int) -> None:
        policy = self.policy
        if policy.max_input_tokens and input_tokens > policy.max_input_tokens:
            raise self._refuse("max_input_tokens", "the prompt is larger than LLM_MAX_INPUT_TOKENS",
                               estimated_input_tokens=input_tokens,
                               limit=policy.max_input_tokens)
        if not (policy.token_budget or policy.cost_budget) or self.ledger is None:
            return
        since = self._now() - timedelta(seconds=policy.budget_window_s)
        spent_tokens, spent_cost = self.ledger.spent(since)
        worst_tokens = input_tokens + policy.max_output_tokens
        if policy.token_budget and spent_tokens + worst_tokens > policy.token_budget:
            raise self._refuse("token_budget", "LLM_TOKEN_BUDGET would be exceeded",
                               spent=spent_tokens, limit=policy.token_budget)
        worst_cost = policy.cost(input_tokens, policy.max_output_tokens)
        if policy.cost_budget and spent_cost + worst_cost > policy.cost_budget:
            raise self._refuse("cost_budget", "LLM_COST_BUDGET would be exceeded",
                               spent=round(spent_cost, 6), limit=policy.cost_budget)

    def _attempt(self, system: str, prompt: str) -> str:
        """One call, with every failure as LlmUnavailableError."""
        try:
            return self.inner.complete(system=system, prompt=prompt)
        except LlmUnavailableError:
            raise
        except Exception as exc:  # a provider bug degrades to the rules
            raise LlmUnavailableError("the LLM adapter failed unexpectedly",
                                      provider=self.provider,
                                      cause=type(exc).__name__) from exc

    def _settle(self, input_tokens: int, text: str, outcome: str) -> calls.Usage:
        usage = calls.take_usage()
        if usage is None:
            usage = calls.Usage(input_tokens, self.policy.tokens(text), estimated=True)
        usage.cost = self.policy.cost(usage.input_tokens, usage.output_tokens)
        metrics.LLM_TOKENS.labels(self.provider, "input").inc(usage.input_tokens)
        metrics.LLM_TOKENS.labels(self.provider, "output").inc(usage.output_tokens)
        metrics.LLM_COST.labels(self.provider).inc(usage.cost)
        if self.ledger is not None:
            self.ledger.record(self.provider, usage, outcome)
        return usage

    def complete(self, *, system: str, prompt: str) -> str:
        calls.note_call(calls.Usage(0, 0), self.provider)
        input_tokens = self.policy.tokens(system) + self.policy.tokens(prompt)
        self._check_budget(input_tokens)
        breaker = breaker_for(self.provider, self.policy)
        try:
            breaker.acquire()
        except LlmCircuitOpenError:
            metrics.LLM_REFUSED.labels(self.provider, "circuit_open").inc()
            raise
        total = calls.Usage(0, 0)
        attempt = 0
        try:
            while True:
                calls.take_usage()
                try:
                    text = self._attempt(system, prompt)
                except LlmUnavailableError:
                    # The request was sent: its input counts against the budget.
                    total = _add(total, self._settle(input_tokens, "", "failed"))
                    if attempt >= self.policy.max_retries:
                        breaker.failure()
                        raise
                    self._sleep(self.policy.retry_backoff_s * (2 ** attempt))
                    attempt += 1
                    continue
                breaker.success()
                total = _add(total, self._settle(input_tokens, text, "ok"))
                return text
        finally:
            calls.note_call(total, self.provider)


def _add(total: calls.Usage, usage: calls.Usage) -> calls.Usage:
    return calls.Usage(total.input_tokens + usage.input_tokens,
                       total.output_tokens + usage.output_tokens,
                       total.estimated or usage.estimated, total.cost + usage.cost)
