"""Member 2 - agentic strategy selection: the LLM works through the evidence with tools before
it commits to a strategy, instead of answering one prompt.

Each step the LLM replies with one JSON object, either a tool call
``{"thought": "...", "action": "<tool>", "input": {...}}`` or its answer
``{"thought": "...", "action": "submit_decision", "input": {"strategy": ..., "confidence": ...,
"rationale": ...}}``. The tool's result is appended to the transcript and the LLM is asked again,
up to DECISION_AGENT_MAX_STEPS calls. The protocol is plain text, so it runs on every LLMPort
provider, and every step goes through ``calls.ask`` (recorded, guarded and budgeted).

What the agent may do is fixed here, not by the LLM:

- every tool is read-only and deterministic: it reads the DecisionPackage Member 1 measured,
  the hard-constraint result and the settings. Nothing is trained, written or fetched;
- ``submit_decision`` must name a strategy the hard constraints allowed. A strategy outside
  that set, or a malformed answer, is returned to the agent as an error so it can correct
  itself; the step limit still applies;
- an unreachable LLM, a refused call (circuit breaker, budget) or an exhausted step limit falls
  back to the deterministic rules, with the reason recorded.

The trace of every step (action, input, a truncated observation) goes into ``Decision.llm``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from oran_adapt.analysis.schemas import DecisionPackage
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.decision.constraints import ConstraintResult
from oran_adapt.decision.fallback import select_strategy_fallback
from oran_adapt.decision.llm_selector import LlmStrategyChoice, _extract_json
from oran_adapt.decision.report import explain_decision
from oran_adapt.decision.schemas import Decision
from oran_adapt.llm import calls
from oran_adapt.llm.prompts import STRATEGY_AGENT, Prompt, get_prompt

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings
    from oran_adapt.llm.client import LlmClient

logger = logging.getLogger(__name__)

FALLBACK_STEP_LIMIT = "agent_step_limit"
SUBMIT = "submit_decision"
# What one observation may add to the transcript, and what the trace keeps of it.
_OBSERVATION_CHARS = 4000
_TRACE_CHARS = 500


@dataclass
class _Context:
    package: DecisionPackage
    constraints: ConstraintResult
    settings: Settings


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _drift_summary(ctx: _Context, _: dict) -> Any:
    p = ctx.package
    return {
        "drift_score": p.drift_event.drift_score,
        "severity": str(p.drift_event.severity) if p.drift_event.severity else None,
        "max_psi": p.max_psi,
        "min_ks_pvalue": p.min_ks_pvalue,
        "reuse_reason": p.reuse_reason,
        "historical_rows": p.historical_data.row_count if p.historical_data else 0,
        "drifted_rows": p.drifted_data.row_count if p.drifted_data else 0,
        "summary": p.drift_summary.model_dump(mode="json") if p.drift_summary else None,
    }


def _feature_shifts(ctx: _Context, args: dict) -> Any:
    top = args.get("top", 10)
    if not isinstance(top, int) or isinstance(top, bool) or top < 1:
        raise ValueError("'top' must be a positive integer")
    shifts = sorted(ctx.package.feature_shifts, key=lambda f: f.psi, reverse=True)
    return {
        "features_total": len(shifts),
        "shifts": [f.model_dump(mode="json") for f in shifts[:top]],
    }


def _version_evaluations(ctx: _Context, _: dict) -> Any:
    reuse = ctx.package.reuse_decision
    return {
        "versions": [
            e.model_dump(mode="json", exclude={"baseline_metrics"})
            for e in ctx.package.version_evaluations
        ],
        "reuse_verdict": reuse.verdict if reuse else None,
        "reuse_reason": reuse.reason if reuse else None,
        "recent_performance": ctx.package.recent_performance,
    }


def _constraints(ctx: _Context, _: dict) -> Any:
    s = ctx.settings
    return {
        "compatible_strategies": [x.value for x in ctx.constraints.compatible_strategies],
        "ruled_out": {k.value: v for k, v in ctx.constraints.rejected.items()},
        "thresholds": {
            "min_drifted_rows": s.decision_min_drifted_rows,
            "full_retrain_psi": s.decision_full_retrain_psi_threshold,
            "gate_mode": s.gate_policy.mode,
            "gate_margin": s.gate_policy.margin,
        },
        "device": "cpu",
    }


def _strategy(args: dict, ctx: _Context) -> Strategy:
    try:
        strategy = Strategy(str(args.get("strategy")))
    except ValueError:
        raise ValueError(
            "'strategy' must be one of "
            + ", ".join(x.value for x in ctx.constraints.compatible_strategies)
        ) from None
    return strategy


def _estimate(ctx: _Context, args: dict) -> Any:
    """What carrying out a strategy would cost and gain, from decision.report: the same
    deterministic explanation every final decision gets."""
    strategy = _strategy(args, ctx)
    draft = Decision(
        model_id=ctx.package.model_id, strategy=strategy, confidence=0.0, rationale="draft",
        compatible_strategies=ctx.constraints.compatible_strategies, source="LLM",
    )
    report = explain_decision(draft, ctx.package, ctx.settings)
    return {
        "strategy": strategy.value,
        "allowed": strategy in ctx.constraints.compatible_strategies,
        "expected_cost": report.expected_cost,
        "expected_improvement": report.expected_improvement,
        "required_data": report.required_data,
        "resource_requirement": report.resource_requirement,
        "fallback_strategy": report.fallback_strategy,
    }


def _rule_recommendation(ctx: _Context, _: dict) -> Any:
    strategy, reason = select_strategy_fallback(ctx.constraints.compatible_strategies)
    return {"strategy": strategy.value, "reason": reason}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[[_Context, dict], Any]
    inputs: dict[str, str] = field(default_factory=dict)


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool("get_drift_summary", "drift score, PSI/KS extremes, row counts and the drift "
             "summary Member 1 computed", _drift_summary),
        Tool("get_feature_shifts", "per-feature shift statistics, largest PSI first",
             _feature_shifts, {"top": "optional positive integer, default 10"}),
        Tool("get_version_evaluations", "how every registered version scored on the current "
             "data, the reuse verdict and recent live performance", _version_evaluations),
        Tool("get_constraints", "the strategies the hard constraints allow, the ones they ruled "
             "out and why, and the thresholds", _constraints),
        Tool("estimate_strategy", "expected cost, expected improvement, required data, "
             "resources and fallback for one strategy", _estimate,
             {"strategy": "a strategy name"}),
        Tool("get_rule_recommendation", "what the deterministic rules would choose, and why",
             _rule_recommendation),
    )
}


def tool_catalogue() -> str:
    lines = []
    for tool in TOOLS.values():
        inputs = ", ".join(f"{k}: {v}" for k, v in tool.inputs.items()) or "none"
        lines.append(f"- {tool.name} (input: {inputs}): {tool.description}")
    lines.append(f"- {SUBMIT} (input: strategy, confidence 0..1, rationale): your final answer")
    return "\n".join(lines)


@dataclass
class AgentOutcome:
    choice: LlmStrategyChoice | None
    fallback_reason: str | None
    prompt: Prompt
    trace: list[dict[str, Any]]

    @property
    def steps(self) -> int:
        return len(self.trace)


def _brief(ctx: _Context) -> str:
    return _json({
        "task": "choose the adaptation strategy for this drifted model",
        "model_id": ctx.package.model_id,
        "framework": ctx.package.framework,
        "model_type": ctx.package.model_type,
        "task_type": ctx.package.task_type,
        "compatible_strategies": [x.value for x in ctx.constraints.compatible_strategies],
        "tools": tool_catalogue(),
    })


def _transcript(brief: str, turns: list[tuple[str, str]], steps_left: int) -> str:
    parts = [f"BRIEF:\n{brief}"]
    for reply, observation in turns:
        parts.append(f"YOU:\n{reply}\nOBSERVATION:\n{observation}")
    parts.append(f"Steps left including this one: {steps_left}. Reply with one JSON object.")
    return "\n\n".join(parts)


def _submit(ctx: _Context, args: dict) -> LlmStrategyChoice:
    choice = LlmStrategyChoice.model_validate_json(json.dumps(args))
    if choice.strategy not in ctx.constraints.compatible_strategies:
        raise ValueError(
            f"{choice.strategy.value} is not allowed; choose one of "
            + ", ".join(x.value for x in ctx.constraints.compatible_strategies)
        )
    return choice


def run_agent(
    client: LlmClient,
    package: DecisionPackage,
    constraints: ConstraintResult,
    settings: Settings,
) -> AgentOutcome:
    """Run the loop; never raises for anything the LLM does."""
    prompt = get_prompt(STRATEGY_AGENT, settings)
    trace: list[dict[str, Any]] = []
    if not constraints.compatible_strategies:
        return AgentOutcome(None, calls.FALLBACK_OUTSIDE_SET, prompt, trace)
    ctx = _Context(package, constraints, settings)
    brief = _brief(ctx)
    turns: list[tuple[str, str]] = []
    max_steps = settings.decision_agent_max_steps
    log = {"model_id": package.model_id, "component": "decision-agent"}

    for step in range(1, max_steps + 1):
        try:
            raw = calls.ask(client, prompt, _transcript(brief, turns, max_steps - step + 1),
                            purpose="decision", step=step)
        except LlmUnavailableError as exc:
            logger.warning("decision agent: LLM call failed, falling back",
                           extra={**log, "step": step, "reason": exc.reason})
            return AgentOutcome(None, exc.reason, prompt, trace)

        entry: dict[str, Any] = {"step": step}
        try:
            reply = _extract_json(raw)
            if not isinstance(reply, dict):
                raise TypeError("the reply must be a JSON object")
            action, args = reply.get("action"), reply.get("input") or {}
            if not isinstance(args, dict):
                raise TypeError("'input' must be a JSON object")
            thought = str(reply.get("thought", ""))[:_TRACE_CHARS]
            entry.update(action=action, input=args, thought=thought)
            if action == SUBMIT:
                choice = _submit(ctx, args)
                trace.append({**entry, "observation": "accepted"})
                return AgentOutcome(choice, None, prompt, trace)
            tool = TOOLS.get(action) if isinstance(action, str) else None
            if tool is None:
                raise ValueError(f"unknown action {action!r}; use one of "
                                 + ", ".join([*TOOLS, SUBMIT]))
            observation = _json(tool.run(ctx, args))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            # Shown to the agent so it can correct itself on its next step.
            detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            observation = _json({"error": detail})
            entry.setdefault("action", None)
            entry["error"] = True

        observation = observation[:_OBSERVATION_CHARS]
        trace.append({**entry, "observation": observation[:_TRACE_CHARS]})
        turns.append((raw.strip()[:_OBSERVATION_CHARS], observation))

    logger.warning("decision agent: step limit reached, falling back",
                   extra={**log, "steps": max_steps})
    return AgentOutcome(None, FALLBACK_STEP_LIMIT, prompt, trace)
