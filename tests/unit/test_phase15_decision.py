"""Phase E (Rule 6): the decision engine keeps the LLM inside deterministic safety constraints
that now include the model's capabilities and the evidence behind the drift, validates the LLM's
JSON strictly, derives confidence from the evidence, and explains every decision with its
available and rejected strategies, capability, drift magnitude and data availability."""

from __future__ import annotations

import json

from oran_adapt.analysis.schemas import (
    DataVersionRef,
    DecisionPackage,
    DriftSummary,
    FeatureShift,
    VersionEvaluation,
)
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.decision.constraints import evaluate_constraints
from oran_adapt.decision.engine import decide
from oran_adapt.decision.llm_selector import select_strategy_via_llm

SETTINGS = Settings(_env_file=None)
CAPS = {"fine_tuning": True, "full_retraining": True, "llm_adapter": False}


class _Llm:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _summary(**overrides) -> DriftSummary:
    values = {
        "features_compared": 2, "affected_features": ["prb_util"], "n_affected": 1,
        "affected_share": 0.5, "max_psi": 0.3, "mean_psi": 0.2, "min_ks_pvalue": 0.001,
        "bonferroni_alpha": 0.025, "significant_features": ["prb_util"], "historical_rows": 400,
        "drifted_rows": 200, "late_rows": 0, "sample_sufficient": True,
        "sample_note": "200 drifted rows (minimum 10)", "previous_version": "2",
        "recent_performance": {"rmse": 3.0}, "capabilities": dict(CAPS),
    }
    values.update(overrides)
    return DriftSummary(**values)


def _package(**overrides) -> DecisionPackage:
    rows = overrides.pop("rows", 200)
    shift = FeatureShift(feature="prb_util", historical_mean=1.0, drifted_mean=2.0,
                         historical_std=0.1, drifted_std=0.1, ks_statistic=0.5,
                         ks_pvalue=0.001, psi=0.3, n_historical=400, n_drifted=rows)
    values = {
        "model_id": "m", "framework": "sklearn",
        "drift_event": DriftEvent(model_id="m", drift_detected=True), "reuse_reason": "shift",
        "drifted_data": DataVersionRef(data_version_id=1, version="d", kind="DRIFTED",
                                       row_count=rows),
        "feature_shifts": [shift], "max_psi": 0.3, "min_ks_pvalue": 0.001,
        "recent_performance": {"rmse": 3.0}, "drift_summary": _summary(drifted_rows=rows),
    }
    values.update(overrides)
    return DecisionPackage(**values)


def _evaluation(version: str, *, live: bool, compatible: bool = True) -> VersionEvaluation:
    return VersionEvaluation(version=version, is_live=live, compatible=compatible)


# ---- hard constraints use capability, sample and version facts --------------------------------
def test_fine_tuning_is_ruled_out_when_the_framework_cannot_fine_tune():
    caps = {"fine_tuning": False, "full_retraining": True, "llm_adapter": False}
    result = evaluate_constraints(
        _package(framework="xgboost", drift_summary=_summary(capabilities=caps)), SETTINGS)

    assert Strategy.FINE_TUNING not in result.compatible_strategies
    assert "no fine-tuning engine" in result.rejected[Strategy.FINE_TUNING]
    assert Strategy.FULL_RETRAINING in result.compatible_strategies


def test_the_llm_adapter_keeps_fine_tuning_open_without_a_native_engine():
    caps = {"fine_tuning": False, "full_retraining": True, "llm_adapter": True}
    result = evaluate_constraints(_package(drift_summary=_summary(capabilities=caps)), SETTINGS)
    assert Strategy.FINE_TUNING in result.compatible_strategies


def test_an_insufficient_sample_forces_insufficient_information():
    small = _summary(sample_sufficient=False, sample_note="only 4 drifted rows; at least 10")
    result = evaluate_constraints(_package(drift_summary=small), SETTINGS)

    assert result.forced_strategy == Strategy.INSUFFICIENT_INFORMATION
    assert "only 4 drifted rows" in result.reason


def test_rollback_needs_a_compatible_earlier_version():
    only_live = [_evaluation("3", live=True), _evaluation("2", live=False, compatible=False)]
    result = evaluate_constraints(_package(version_evaluations=only_live), SETTINGS)
    assert Strategy.ROLLBACK not in result.compatible_strategies
    assert "no compatible earlier version" in result.rejected[Strategy.ROLLBACK]

    with_target = [_evaluation("3", live=True), _evaluation("2", live=False)]
    ok = evaluate_constraints(_package(version_evaluations=with_target), SETTINGS)
    assert Strategy.ROLLBACK in ok.compatible_strategies


# ---- the LLM stays inside the constraints and must answer in strict JSON -----------------------
def test_llm_answer_with_unexpected_fields_is_rejected():
    body = json.dumps({"strategy": "FULL_RETRAINING", "confidence": 0.9, "rationale": "x",
                       "execute": "rm -rf /"})
    assert select_strategy_via_llm(_Llm(body), _package(), [Strategy.FULL_RETRAINING]) is None


def test_llm_answer_in_natural_language_is_never_parsed_into_a_strategy():
    llm = _Llm("I think FULL_RETRAINING is best here, with high confidence.")
    assert select_strategy_via_llm(llm, _package(), [Strategy.FULL_RETRAINING]) is None


def test_llm_sees_the_drift_summary():
    llm = _Llm(json.dumps({"strategy": "FULL_RETRAINING", "confidence": 0.7, "rationale": "x"}))
    select_strategy_via_llm(llm, _package(), [Strategy.FULL_RETRAINING])
    sent = json.loads(llm.prompts[0])
    assert sent["drift_summary"]["significant_features"] == ["prb_util"]


def test_llm_choice_outside_the_capability_constraints_falls_back_deterministically():
    caps = {"fine_tuning": False, "full_retraining": True, "llm_adapter": False}
    llm = _Llm(json.dumps({"strategy": "FINE_TUNING", "confidence": 0.95, "rationale": "cheap"}))

    decision = decide(_package(framework="xgboost", drift_summary=_summary(capabilities=caps)),
                      SETTINGS, llm)

    assert decision.source == "FALLBACK"
    assert decision.strategy == Strategy.FULL_RETRAINING


# ---- confidence comes from the evidence ---------------------------------------------------------
def test_fallback_confidence_grows_with_sample_size_and_significance():
    strong = decide(_package(rows=200), SETTINGS, None)
    thin = decide(_package(rows=20), SETTINGS, None)
    unsupported = decide(
        _package(drift_summary=_summary(significant_features=[], affected_features=[],
                                        n_affected=0)),
        SETTINGS, None)

    assert strong.confidence == 1.0
    assert thin.confidence < strong.confidence
    assert unsupported.confidence < strong.confidence
    assert 0.0 < thin.confidence and 0.0 < unsupported.confidence


def test_llm_confidence_is_capped_by_the_evidence():
    llm = _Llm(json.dumps({"strategy": "FULL_RETRAINING", "confidence": 0.99, "rationale": "x"}))
    decision = decide(_package(rows=20), SETTINGS, llm)

    assert decision.source == "LLM"
    assert decision.confidence == decide(_package(rows=20), SETTINGS, None).confidence
    assert decision.evidence["llm_confidence"] == 0.99


# ---- the decision record explains itself --------------------------------------------------------
def test_decision_record_explains_capability_magnitude_and_data_availability():
    decision = decide(_package(), SETTINGS, None)

    assert decision.compatible_strategies  # available strategies
    assert decision.rejected_strategies  # and the ones passed over, with reasons
    assert decision.evidence["model_capability"] == CAPS
    assert decision.evidence["drift_magnitude"] == {
        "max_psi": 0.3, "mean_psi": 0.2, "min_ks_pvalue": 0.001, "n_affected": 1,
        "affected_share": 0.5, "significant_features": ["prb_util"]}
    assert decision.evidence["data_availability"] == {
        "historical_rows": 400, "drifted_rows": 200, "late_rows": 0,
        "sample_sufficient": True, "note": "200 drifted rows (minimum 10)"}
