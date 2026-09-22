"""Member 2 - deterministic fallback: what to decide when there is no LLM configured, or the LLM
call failed or returned something we can't trust. Must never raise and must always return a
strategy from the compatible set it is given (or NO_ACTION if that set is empty)."""

from __future__ import annotations

from oran_adapt.core.enums import Strategy


def select_strategy_fallback(compatible: list[Strategy]) -> tuple[Strategy, str]:
    if Strategy.FINE_TUNING in compatible:
        return Strategy.FINE_TUNING, "fallback rule: fine-tuning is compatible and least costly"
    if Strategy.FULL_RETRAINING in compatible:
        return (
            Strategy.FULL_RETRAINING,
            "fallback rule: fine-tuning ruled out, full retraining is compatible",
        )
    if Strategy.ROLLBACK in compatible:
        return Strategy.ROLLBACK, "fallback rule: only rollback is compatible"
    return Strategy.NO_ACTION, "fallback rule: no adaptation strategy is compatible"
