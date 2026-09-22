"""Phase 3: Member 2 (decision engine) - hard constraints, LLM selection, deterministic fallback."""

from __future__ import annotations

import json

import pytest

from oran_adapt.analysis.schemas import DecisionPackage, FeatureShift
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.decision.constraints import evaluate_constraints
from oran_adapt.decision.engine import decide
from oran_adapt.decision.fallback import select_strategy_fallback
from oran_adapt.decision.llm_selector import select_strategy_via_llm


def _shift(psi: float = 0.2, ks_pvalue: float = 0.01) -> FeatureShift:
    return FeatureShift(
        feature="prb_util",
        historical_mean=1.0,
        drifted_mean=2.0,
        historical_std=0.1,
        drifted_std=0.1,
        ks_statistic=0.5,
        ks_pvalue=ks_pvalue,
        psi=psi,
    )


def _package(**overrides) -> DecisionPackage:
    defaults = {
        "model_id": "ran-kpi-v1",
        "model_type": "regression",
        "framework": "sklearn",
        "drift_event": DriftEvent(model_id="ran-kpi-v1", drift_detected=True),
        "reuse_reason": "significant statistical shift in: prb_util",
        "historical_data": None,
        "drifted_data": None,
        "feature_shifts": [_shift()],
        "max_psi": 0.2,
        "min_ks_pvalue": 0.01,
        "merge_overlap_count": 0,
        "recent_performance": {"rmse": 4.5},
    }
    defaults.update(overrides)
    return DecisionPackage(**defaults)


def _drifted_ref(row_count: int):
    from oran_adapt.analysis.schemas import DataVersionRef

    return DataVersionRef(data_version_id=1, version="drift-1", kind="DRIFTED", row_count=row_count)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


# ---- hard constraints --------------------------------------------------------------------------
def test_constraints_force_no_compatible_strategy_for_unsupported_framework(settings) -> None:
    package = _package(framework="cobol-model", drifted_data=_drifted_ref(50))
    result = evaluate_constraints(package, settings)
    assert result.forced_strategy == Strategy.NO_COMPATIBLE_STRATEGY
    assert "cobol-model" in result.reason


def test_constraints_force_no_compatible_strategy_when_framework_missing(settings) -> None:
    package = _package(framework=None, drifted_data=_drifted_ref(50))
    result = evaluate_constraints(package, settings)
    assert result.forced_strategy == Strategy.NO_COMPATIBLE_STRATEGY


def test_constraints_force_insufficient_information_for_too_little_drifted_data(settings) -> None:
    package = _package(drifted_data=_drifted_ref(2))
    result = evaluate_constraints(package, settings)
    assert result.forced_strategy == Strategy.INSUFFICIENT_INFORMATION
    assert "2 drifted rows" in result.reason


def test_constraints_force_insufficient_information_when_no_feature_shifts(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50), feature_shifts=[])
    result = evaluate_constraints(package, settings)
    assert result.forced_strategy == Strategy.INSUFFICIENT_INFORMATION


def test_constraints_exclude_fine_tuning_above_catastrophic_psi(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50), max_psi=0.9, feature_shifts=[_shift(psi=0.9)])
    result = evaluate_constraints(package, settings)
    assert result.forced_strategy is None
    assert Strategy.FINE_TUNING not in result.compatible_strategies
    assert Strategy.FULL_RETRAINING in result.compatible_strategies


def test_constraints_include_fine_tuning_below_catastrophic_psi(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50), max_psi=0.2)
    result = evaluate_constraints(package, settings)
    assert Strategy.FINE_TUNING in result.compatible_strategies


def test_constraints_exclude_rollback_without_recent_performance(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50), recent_performance={})
    result = evaluate_constraints(package, settings)
    assert Strategy.ROLLBACK not in result.compatible_strategies


def test_constraints_include_rollback_with_recent_performance(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50), recent_performance={"rmse": 4.5})
    result = evaluate_constraints(package, settings)
    assert Strategy.ROLLBACK in result.compatible_strategies


# ---- deterministic fallback ----------------------------------------------------------------
def test_fallback_prefers_fine_tuning_when_compatible() -> None:
    strategy, reason = select_strategy_fallback(
        [Strategy.FINE_TUNING, Strategy.FULL_RETRAINING, Strategy.ROLLBACK]
    )
    assert strategy == Strategy.FINE_TUNING
    assert reason


def test_fallback_falls_to_full_retraining_when_fine_tuning_excluded() -> None:
    strategy, _ = select_strategy_fallback([Strategy.FULL_RETRAINING, Strategy.ROLLBACK])
    assert strategy == Strategy.FULL_RETRAINING


def test_fallback_falls_to_rollback_when_only_option() -> None:
    strategy, _ = select_strategy_fallback([Strategy.ROLLBACK])
    assert strategy == Strategy.ROLLBACK


def test_fallback_no_action_when_nothing_compatible() -> None:
    strategy, _ = select_strategy_fallback([])
    assert strategy == Strategy.NO_ACTION


# ---- LLM selection (fake client, no network) -------------------------------------------------
class FakeLlmClient:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


_COMPATIBLE = [Strategy.FINE_TUNING, Strategy.FULL_RETRAINING, Strategy.ROLLBACK]


def test_llm_selector_accepts_valid_response_within_compatible_set() -> None:
    client = FakeLlmClient(
        response=json.dumps(
            {"strategy": "FULL_RETRAINING", "confidence": 0.8, "rationale": "large shift"}
        )
    )
    choice = select_strategy_via_llm(client, _package(), _COMPATIBLE)
    assert choice is not None
    assert choice.strategy == Strategy.FULL_RETRAINING
    assert choice.confidence == 0.8
    assert client.calls  # prompt was actually sent


def test_llm_selector_strips_markdown_fences() -> None:
    body = json.dumps({"strategy": "FINE_TUNING", "confidence": 0.6, "rationale": "moderate"})
    client = FakeLlmClient(response=f"```json\n{body}\n```")
    choice = select_strategy_via_llm(client, _package(), _COMPATIBLE)
    assert choice is not None
    assert choice.strategy == Strategy.FINE_TUNING


def test_llm_selector_returns_none_on_provider_failure() -> None:
    client = FakeLlmClient(error=LlmUnavailableError("boom"))
    assert select_strategy_via_llm(client, _package(), _COMPATIBLE) is None


def test_llm_selector_returns_none_on_invalid_json() -> None:
    client = FakeLlmClient(response="not json at all")
    assert select_strategy_via_llm(client, _package(), _COMPATIBLE) is None


def test_llm_selector_returns_none_when_strategy_outside_compatible_set() -> None:
    client = FakeLlmClient(
        response=json.dumps({"strategy": "ROLLBACK", "confidence": 0.9, "rationale": "x"})
    )
    choice = select_strategy_via_llm(client, _package(), [Strategy.FINE_TUNING])
    assert choice is None


def test_llm_selector_returns_none_when_compatible_set_is_empty() -> None:
    client = FakeLlmClient(response="{}")
    assert select_strategy_via_llm(client, _package(), []) is None
    assert client.calls == []  # never even called out


# ---- end-to-end decide() -----------------------------------------------------------------------
def test_decide_hard_constraint_short_circuits_without_llm(settings) -> None:
    package = _package(framework="cobol-model", drifted_data=_drifted_ref(50))
    decision = decide(package, settings, llm_client=None)
    assert decision.strategy == Strategy.NO_COMPATIBLE_STRATEGY
    assert decision.source == "HARD_CONSTRAINT"
    assert decision.compatible_strategies == []


def test_decide_uses_fallback_when_no_llm_configured(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50))
    decision = decide(package, settings, llm_client=None)
    assert decision.source == "FALLBACK"
    assert decision.strategy == Strategy.FINE_TUNING


def test_decide_uses_llm_when_available_and_valid(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50))
    client = FakeLlmClient(
        response=json.dumps(
            {"strategy": "FULL_RETRAINING", "confidence": 0.75, "rationale": "shift is broad"}
        )
    )
    decision = decide(package, settings, llm_client=client)
    assert decision.source == "LLM"
    assert decision.strategy == Strategy.FULL_RETRAINING
    assert decision.confidence == 0.75


def test_decide_falls_back_when_llm_fails(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50))
    client = FakeLlmClient(error=LlmUnavailableError("timeout"))
    decision = decide(package, settings, llm_client=client)
    assert decision.source == "FALLBACK"
    assert decision.strategy == Strategy.FINE_TUNING


def test_decide_result_is_json_serializable(settings) -> None:
    package = _package(drifted_data=_drifted_ref(50))
    decision = decide(package, settings, llm_client=None)
    json.dumps(decision.model_dump(mode="json"))
