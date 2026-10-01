"""Online health of a rollout: the candidate arm against the stable arm, on the metrics the
rollout metrics adapter reports (DeliveryPolicy.health), and the A/B significance test.

Both are pure functions of the observations and the policy, so every verdict can be recomputed
from the stored policy and the same observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import fmean
from typing import Final, Literal

from oran_adapt.core.policies import DeliveryPolicy, HealthRule
from oran_adapt.ports import ArmStats

Status = Literal["healthy", "breach", "insufficient"]
HEALTHY: Final = "healthy"
BREACH: Final = "breach"
INSUFFICIENT: Final = "insufficient"


@dataclass(frozen=True)
class HealthVerdict:
    """``status`` is ``healthy``, ``breach`` (at least one rule broken) or ``insufficient``
    (too few observations, or a required metric missing on an arm, to judge yet)."""

    status: Status
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons), "detail": self.detail}


def _worse_by(rule: HealthRule, candidate: float, stable: float) -> float:
    """How much worse the candidate is, in the metric's units (negative: better)."""
    return candidate - stable if rule.direction == "lower" else stable - candidate


def _ratio_breached(rule: HealthRule, candidate: float, stable: float) -> bool:
    assert rule.max_ratio is not None
    if rule.direction == "lower":
        return stable > 0 and candidate > rule.max_ratio * stable
    return candidate > 0 and stable > rule.max_ratio * candidate


def _beyond_limit(rule: HealthRule, candidate: float) -> bool:
    assert rule.limit is not None
    return candidate > rule.limit if rule.direction == "lower" else candidate < rule.limit


def evaluate_health(
    policy: DeliveryPolicy, stable: ArmStats, candidate: ArmStats
) -> HealthVerdict:
    """Judge the candidate arm. Nothing is judged before both arms have ``min_samples``
    requests; then every rule is checked and any broken one is a breach."""
    if candidate.count < policy.min_samples or stable.count < policy.min_samples:
        return HealthVerdict(
            INSUFFICIENT,
            [(f"{min(candidate.count, stable.count)} of {policy.min_samples} requests observed "
              "on the least-observed arm (DELIVERY_POLICY.min_samples)")],
        )
    reasons: list[str] = []
    missing: list[str] = []
    detail: dict[str, dict[str, float | None]] = {}
    for rule in policy.health:
        c_values = candidate.samples.get(rule.metric) or []
        s_values = stable.samples.get(rule.metric) or []
        c_mean = fmean(c_values) if c_values else None
        s_mean = fmean(s_values) if s_values else None
        detail[rule.metric] = {"candidate": c_mean, "stable": s_mean}
        if c_mean is None:
            if rule.required:
                missing.append(f"{rule.metric} (candidate)")
            continue
        if rule.limit is not None and _beyond_limit(rule, c_mean):
            reasons.append(f"{rule.metric} {c_mean:.6g} is beyond its limit {rule.limit:g}")
        if rule.max_degradation is None and rule.max_ratio is None:
            continue
        if s_mean is None:
            if rule.required:
                missing.append(f"{rule.metric} (stable)")
            continue
        worse = _worse_by(rule, c_mean, s_mean)
        detail[rule.metric]["worse_by"] = worse
        if rule.max_degradation is not None and worse > rule.max_degradation:
            reasons.append(
                f"{rule.metric} {c_mean:.6g} is {worse:.6g} worse than stable's {s_mean:.6g} "
                f"(allowed {rule.max_degradation:g})"
            )
        if rule.max_ratio is not None and _ratio_breached(rule, c_mean, s_mean):
            reasons.append(
                f"{rule.metric} {c_mean:.6g} against stable's {s_mean:.6g} exceeds the "
                f"ratio {rule.max_ratio:g}"
            )
    if reasons:
        return HealthVerdict(BREACH, reasons, detail)
    if missing:
        return HealthVerdict(
            INSUFFICIENT, [f"no samples yet of required metric {m}" for m in missing], detail
        )
    return HealthVerdict(HEALTHY, [], detail)


@dataclass(frozen=True)
class AbResult:
    """The A/B verdict on ``metric``: ``better``, ``worse`` or ``inconclusive``."""

    verdict: Literal["better", "worse", "inconclusive"]
    metric: str
    p_value: float | None
    candidate_mean: float | None
    stable_mean: float | None
    reason: str

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict, "metric": self.metric, "p_value": self.p_value,
            "candidate_mean": self.candidate_mean, "stable_mean": self.stable_mean,
            "reason": self.reason,
        }


def ab_test(policy: DeliveryPolicy, stable: ArmStats, candidate: ArmStats) -> AbResult:
    """Welch's t-test (unequal variances) of the candidate's ``ab_metric`` samples against
    stable's. Significant at ``ab_confidence`` and in the better direction: ``better``;
    significant and worse: ``worse``; otherwise (or too few samples) ``inconclusive``."""
    from scipy import stats

    metric = policy.ab_metric
    c_values = candidate.samples.get(metric) or []
    s_values = stable.samples.get(metric) or []
    c_mean = fmean(c_values) if c_values else None
    s_mean = fmean(s_values) if s_values else None
    enough = (
        min(candidate.count, stable.count) >= policy.min_samples
        and len(c_values) >= 2 and len(s_values) >= 2
    )
    if not enough or c_mean is None or s_mean is None:
        return AbResult("inconclusive", metric, None, c_mean, s_mean,
                        f"too few observations of {metric} for a significance test")
    result = stats.ttest_ind(c_values, s_values, equal_var=False)
    p_value = float(result.pvalue)
    if math.isnan(p_value):  # both arms constant: identical means test as no difference
        p_value = 1.0 if c_mean == s_mean else 0.0
    alpha = 1.0 - policy.ab_confidence
    improvement = s_mean - c_mean if policy.ab_direction == "lower" else c_mean - s_mean
    if p_value < alpha and improvement > 0:
        verdict: Literal["better", "worse", "inconclusive"] = "better"
    elif p_value < alpha and improvement < 0:
        verdict = "worse"
    else:
        verdict = "inconclusive"
    return AbResult(
        verdict, metric, p_value, c_mean, s_mean,
        f"{metric}: candidate {c_mean:.6g} vs stable {s_mean:.6g}, Welch p={p_value:.4g} "
        f"(alpha {alpha:.4g}) -> {verdict}",
    )
