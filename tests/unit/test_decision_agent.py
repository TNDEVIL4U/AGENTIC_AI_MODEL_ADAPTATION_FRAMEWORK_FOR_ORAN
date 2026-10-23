"""The agentic decision engine (decision.agent): a scripted LLM calls read-only tools over the
evidence, then submits; the hard constraints, the step limit and the fallback still decide what
is allowed. Nothing here reaches a real LLM."""

from __future__ import annotations

import json

import pytest

from oran_adapt.analysis.schemas import (
    DataVersionRef,
    DecisionPackage,
    DriftSummary,
    FeatureShift,
)
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import LlmCircuitOpenError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.decision import agent
from oran_adapt.decision.constraints import evaluate_constraints
from oran_adapt.decision.engine import decide
from oran_adapt.llm import calls

pytestmark = pytest.mark.smoke


def _shift(feature: str, psi: float) -> FeatureShift:
    return FeatureShift(feature=feature, historical_mean=1.0, drifted_mean=2.0,
                        historical_std=0.1, drifted_std=0.1, ks_statistic=0.5,
                        ks_pvalue=0.01, psi=psi)


def _package(max_psi: float = 0.2) -> DecisionPackage:
    return DecisionPackage(
        model_id="ran-kpi-v1", model_type="regression", framework="sklearn",
        drift_event=DriftEvent(model_id="ran-kpi-v1", drift_detected=True),
        reuse_reason="significant statistical shift in: prb_util",
        drifted_data=DataVersionRef(data_version_id=1, version="drift-1", kind="DRIFTED",
                                    row_count=500),
        feature_shifts=[_shift("prb_util", max_psi), _shift("cqi", 0.05)],
        max_psi=max_psi, min_ks_pvalue=0.01, recent_performance={"rmse": 4.5},
    )


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, decision_agent_enabled=True, **overrides)


def _call(action: str, **inputs) -> str:
    return json.dumps({"thought": f"next: {action}", "action": action, "input": inputs})


def _submit(strategy: str, confidence: float = 0.8) -> str:
    return _call("submit_decision", strategy=strategy, confidence=confidence,
                 rationale="the tools show a moderate shift in prb_util")


class ScriptedLlm:
    """Replies with ``replies`` in order and keeps every prompt it was sent."""

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.systems.append(system)
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_agent_investigates_with_tools_then_submits() -> None:
    llm = ScriptedLlm(_call("get_constraints"), _call("get_feature_shifts", top=1),
                      _call("estimate_strategy", strategy="FINE_TUNING"), _submit("FINE_TUNING"))
    with calls.recording() as recorded:
        decision = decide(_package(), _settings(), llm)

    assert decision.source == "LLM"
    assert decision.strategy == Strategy.FINE_TUNING
    assert decision.llm["mode"] == "agent" and decision.llm["used"] is True
    assert decision.llm["prompt_id"] == "strategy-agent"
    assert [t["action"] for t in decision.llm["trace"]] == [
        "get_constraints", "get_feature_shifts", "estimate_strategy", "submit_decision"]
    assert len(recorded) == 4 and all(c.extra["purpose"] == "decision" for c in recorded)
    # Each step sees what the tools returned before.
    assert "FULL_RETRAINING" in llm.prompts[1]  # from get_constraints
    assert '"features_total": 2' in llm.prompts[2] and "cqi" not in llm.prompts[2].split(
        "OBSERVATION:")[-1]
    assert '"expected_cost"' in llm.prompts[3]
    # The explanation is still computed deterministically.
    assert decision.expected_cost["level"] == "LOW"


def test_agent_cannot_submit_a_strategy_the_constraints_ruled_out() -> None:
    # PSI above the full-retrain threshold rules out fine-tuning; the agent corrects itself.
    llm = ScriptedLlm(_submit("FINE_TUNING"), _submit("FULL_RETRAINING"))
    decision = decide(_package(max_psi=0.9), _settings(), llm)

    assert decision.strategy == Strategy.FULL_RETRAINING and decision.source == "LLM"
    first = decision.llm["trace"][0]
    assert first["error"] is True and "not allowed" in first["observation"]
    assert "not allowed" in llm.prompts[1]


def test_malformed_replies_and_unknown_tools_are_errors_the_agent_sees() -> None:
    llm = ScriptedLlm("not json at all", _call("drop_table"), _call("get_feature_shifts", top=0),
                      _submit("ROLLBACK", confidence=0.6))
    decision = decide(_package(), _settings(), llm)

    assert decision.strategy == Strategy.ROLLBACK
    assert [t.get("error", False) for t in decision.llm["trace"]] == [True, True, True, False]
    assert "unknown action" in llm.prompts[2]
    assert "positive integer" in llm.prompts[3]


def test_step_limit_falls_back_to_the_rules() -> None:
    llm = ScriptedLlm(*[_call("get_drift_summary")] * 3)
    with calls.recording() as recorded:
        decision = decide(_package(), _settings(decision_agent_max_steps=3), llm)

    assert decision.source == "FALLBACK" and decision.strategy == Strategy.FINE_TUNING
    assert decision.llm["fallback_reason"] == agent.FALLBACK_STEP_LIMIT
    assert decision.llm["steps"] == 3 and len(recorded) == 3


def test_an_unreachable_llm_falls_back_at_once() -> None:
    llm = ScriptedLlm(_call("get_constraints"),
                      LlmCircuitOpenError("down"))
    decision = decide(_package(), _settings(), llm)

    assert decision.source == "FALLBACK"
    assert decision.llm["fallback_reason"] == "circuit_open"
    assert decision.llm["steps"] == 1


def test_confidence_is_capped_by_the_evidence() -> None:
    # 50 drifted rows are far short of full-confidence evidence.
    package = _package().model_copy(update={"drift_summary": DriftSummary(
        drifted_rows=50, sample_sufficient=True, significant_features=["prb_util"])})
    decision = decide(package, _settings(), ScriptedLlm(_submit("FINE_TUNING", 1.0)))
    assert decision.confidence < 1.0
    assert decision.evidence["llm_confidence"] == 1.0


def test_agent_is_off_by_default_and_the_single_prompt_is_used() -> None:
    reply = json.dumps({"strategy": "FINE_TUNING", "confidence": 0.7, "rationale": "shift"})
    llm = ScriptedLlm(reply)
    decision = decide(_package(), Settings(_env_file=None), llm)

    assert decision.llm["mode"] == "single" and decision.llm["prompt_id"] == "strategy-selection"
    assert len(llm.prompts) == 1


def test_tools_are_read_only_over_the_package() -> None:
    package, settings = _package(), _settings()
    before = package.model_dump()
    constraints = evaluate_constraints(package, settings)
    ctx = agent._Context(package, constraints, settings)
    for tool in agent.TOOLS.values():
        args = {"strategy": "ROLLBACK"} if "strategy" in tool.inputs else {}
        json.dumps(tool.run(ctx, args), default=str)
    assert package.model_dump() == before
    assert "submit_decision" in agent.tool_catalogue()
