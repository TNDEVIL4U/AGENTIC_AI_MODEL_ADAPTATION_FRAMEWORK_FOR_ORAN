"""Conformance suite for ``LLMPort`` adapters (an LLM provider).

Each check takes the adapter pointed at a working endpoint and a context, and raises
ConformanceFailure on a deviation::

    @pytest.mark.parametrize("check", sorted(LLM_CHECKS))
    def test_my_llm(check):
        LLM_CHECKS[check](MyLlm(...), LlmContext(unreachable=lambda: MyLlm(dead_url), ...))

docs/adapters/llm.md explains each rule.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable
from dataclasses import dataclass

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.llm import calls
from oran_adapt.ports import LLMPort

_SYSTEM = "You are a conformance check. Reply with a short sentence."
_PROMPT = "Say hello."


@dataclass
class LlmContext:
    unreachable: Callable[[], LLMPort]
    """Builds the same adapter pointed at an endpoint nothing listens on."""
    secret: str | None = None
    """The API key the adapters were built with: no error may repeat it."""
    reports_usage: bool = True
    """Whether the provider returns token counts (the adapter must then report them)."""
    expected_reply: str | None = None
    """The exact text the endpoint answers with, when it is a test double."""


def _complete(port: LLMPort) -> str:
    return port.complete(system=_SYSTEM, prompt=_PROMPT)


def _failure(ctx: LlmContext) -> LlmUnavailableError:
    port = ctx.unreachable()
    try:
        _complete(port)
    except LlmUnavailableError as exc:
        return exc
    except Exception as exc:
        raise ConformanceFailure(
            f"an unreachable endpoint raised {type(exc).__name__}, not LlmUnavailableError"
        ) from exc
    raise ConformanceFailure("an unreachable endpoint returned a completion")


def check_protocol(port: LLMPort, ctx: LlmContext) -> None:
    expect(isinstance(port, LLMPort), f"{type(port).__name__} does not implement LLMPort")


def check_returns_text(port: LLMPort, ctx: LlmContext) -> None:
    text = _complete(port)
    expect(isinstance(text, str) and text != "", "complete() returned no text")
    if ctx.expected_reply is not None:
        expect(text == ctx.expected_reply, f"complete() returned {text!r}, not the reply sent")


def check_failures_typed(port: LLMPort, ctx: LlmContext) -> None:
    error = _failure(ctx)
    expect(isinstance(error.reason, str) and error.reason != "",
           "LlmUnavailableError carries no fallback reason")


def check_errors_do_not_echo_key(port: LLMPort, ctx: LlmContext) -> None:
    if not ctx.secret:
        return
    error = _failure(ctx)
    expect(ctx.secret not in f"{error} {error.to_dict()!r}",
           "the LlmUnavailableError repeats the API key")


def check_pickles(port: LLMPort, ctx: LlmContext) -> None:
    try:
        copy = pickle.loads(pickle.dumps(port))
    except Exception as exc:
        raise ConformanceFailure(f"the adapter does not pickle: {type(exc).__name__}") from exc
    text = _complete(copy)
    expect(isinstance(text, str) and text != "", "the unpickled adapter returned no text")


def check_usage_reported(port: LLMPort, ctx: LlmContext) -> None:
    calls.take_usage()
    _complete(port)
    usage = calls.take_usage()
    if ctx.reports_usage:
        expect(usage is not None, "the provider returns token counts but the adapter did not "
                                  "report them (llm.calls.report_usage)")
    if usage is not None:
        expect(usage.input_tokens >= 0 and usage.output_tokens >= 0,
               f"negative token counts reported: {usage}")


LLM_CHECKS: dict[str, Callable[[LLMPort, LlmContext], None]] = {
    "protocol": check_protocol,
    "returns_text": check_returns_text,
    "failures_typed": check_failures_typed,
    "errors_do_not_echo_key": check_errors_do_not_echo_key,
    "pickles": check_pickles,
    "usage_reported": check_usage_reported,
}


def run_llm(port: LLMPort, ctx: LlmContext) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in LLM_CHECKS.values():
        check(port, ctx)
    return list(LLM_CHECKS)
