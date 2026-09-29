"""Versioned policies: the validation gate's (GATE_POLICY) and progressive delivery's
(DELIVERY_POLICY). Each is one structured config value, set inline (a TOML table or a JSON
object in the environment) or from a file (GATE_POLICY_FILE / DELIVERY_POLICY_FILE, TOML or
JSON). The values below are defaults; no threshold is written anywhere else in the code.

Every decision records the policy's ``version`` and ``policy_hash`` (a SHA-256 of its canonical
JSON), so a verdict can always be traced to the exact thresholds that produced it.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from oran_adapt.core.errors import ConfigurationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SliceGuard(_Strict):
    """Per-slice regression check: for every value of each column with at least ``min_rows``
    held-out rows, the candidate's primary metric may be at most ``max_drop`` worse than the
    incumbent's (a fraction of the incumbent's slice value when ``relative``)."""

    columns: list[str] = Field(default_factory=list)
    max_drop: float = Field(0.05, ge=0)
    relative: bool = False
    min_rows: int = Field(30, ge=1)
    # A configured column missing from the held-out data: reject (True) or record it skipped.
    require_columns: bool = False


class CalibrationGuard(_Strict):
    """Binary classifiers with probabilities: expected calibration error over ``bins`` bins.
    The candidate's ECE may exceed the incumbent's by at most ``max_increase`` and, when set,
    must stay under ``max_ece``."""

    enabled: bool = True
    bins: int = Field(10, ge=2)
    max_increase: float = Field(0.05, ge=0)
    max_ece: float | None = Field(None, ge=0, le=1)


class LatencyGuard(_Strict):
    """Median prediction time over ``repeats`` calls on up to ``max_rows`` rows. The candidate
    may take at most ``max_ratio`` times the incumbent's time, measured against at least
    ``floor_ms`` so timer noise on tiny models cannot reject."""

    enabled: bool = True
    max_ratio: float = Field(3.0, gt=0)
    floor_ms: float = Field(10.0, ge=0)
    repeats: int = Field(3, ge=1)
    max_rows: int = Field(1000, ge=1)


class SizeGuard(_Strict):
    """Serialized size: at most ``max_ratio`` times the incumbent's (measured against at least
    ``floor_bytes``) and, when set, at most ``max_bytes``."""

    enabled: bool = True
    max_ratio: float = Field(5.0, gt=0)
    floor_bytes: int = Field(1_048_576, ge=0)
    max_bytes: int | None = Field(None, ge=1)


class GatePolicy(_Strict):
    """Whether a candidate may replace the incumbent (validation.gate).

    The primary metric is compared with a paired bootstrap over the held-out rows: the
    candidate's improvement (oriented so that positive is better) gets a two-sided
    ``confidence`` interval. ``superiority`` accepts only when the whole interval lies above
    ``margin``, so an interval that includes zero is rejected. ``non_inferiority`` accepts when
    it lies above ``-margin``. ``margin_relative`` makes the margin a fraction of the
    incumbent's value (for error metrics with no fixed scale). Guardrails reject on their own,
    whatever the primary metric says."""

    version: str = Field("1", min_length=1)
    mode: Literal["superiority", "non_inferiority"] = "superiority"
    margin: float = Field(0.0, ge=0)
    margin_relative: bool = False
    confidence: float = Field(0.95, gt=0.5, lt=1)
    resamples: int = Field(1000, ge=100)
    seed: int = 0
    # Rows the bootstrap samples from (a seeded subsample above this); bounds memory and time.
    max_rows: int = Field(5000, ge=10)
    # Resamples drawn per vectorised batch (memory: batch * rows indices).
    batch: int = Field(50, ge=1)
    # Silhouette needs pairwise distances: per-row values are computed on at most this many rows.
    pairwise_max_rows: int = Field(2000, ge=10)
    slices: SliceGuard = Field(default_factory=SliceGuard)
    calibration: CalibrationGuard = Field(default_factory=CalibrationGuard)
    latency: LatencyGuard = Field(default_factory=LatencyGuard)
    size: SizeGuard = Field(default_factory=SizeGuard)
    # Secondary metric -> the largest drop allowed (oriented, in the metric's units).
    metric_guards: dict[str, float] = Field(default_factory=dict)

    @field_validator("metric_guards")
    @classmethod
    def _non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        if any(v < 0 for v in value.values()):
            raise ValueError("metric_guards limits must be >= 0")
        return value


class HealthRule(_Strict):
    """One online health check of the candidate arm against the stable arm, on metrics the
    rollout metrics adapter reports. ``direction`` says which way is better. A rule breaches
    when the candidate's mean is beyond ``limit``, more than ``max_degradation`` worse than
    stable's (in the metric's units), or worse by more than the ratio ``max_ratio``."""

    metric: str = Field(min_length=1)
    direction: Literal["lower", "higher"] = "lower"
    limit: float | None = None
    max_degradation: float | None = Field(None, ge=0)
    max_ratio: float | None = Field(None, gt=0)
    # Without samples of a required metric on both arms the rollout cannot decide yet.
    required: bool = True


def _default_health() -> list[HealthRule]:
    return [
        HealthRule(metric="error_rate", direction="lower", max_degradation=0.01),
        HealthRule(metric="latency_p95_ms", direction="lower", max_ratio=1.5, required=False),
    ]


class DeliveryPolicy(_Strict):
    """How a validated candidate reaches traffic (delivery.controller). DELIVERY_STRATEGY
    picks the strategy; this holds its thresholds and timings."""

    version: str = Field("1", min_length=1)
    # Observations each arm needs before any health verdict or step.
    min_samples: int = Field(100, ge=1)
    health: list[HealthRule] = Field(default_factory=_default_health)
    # Shadow: the candidate scores mirrored traffic and serves none.
    shadow_max_s: float = Field(86400.0, gt=0)
    shadow_then: Literal["promote", "canary", "manual"] = "manual"
    # Canary: candidate traffic percentages, each held for canary_step_hold_s; the last is 100.
    canary_steps: list[int] = Field(default_factory=lambda: [5, 25, 50, 100])
    canary_step_hold_s: float = Field(600.0, ge=0)
    # A/B: a fixed split for ab_duration_s, then a Welch t-test on ab_metric.
    ab_percent: int = Field(50, ge=1, le=99)
    ab_duration_s: float = Field(3600.0, gt=0)
    ab_metric: str = Field("error_rate", min_length=1)
    ab_direction: Literal["lower", "higher"] = "lower"
    ab_confidence: float = Field(0.95, gt=0.5, lt=1)
    ab_inconclusive: Literal["rollback", "promote", "manual"] = "rollback"
    # Manual approval: an approver has approval_ttl_s; then approval_then runs.
    approval_ttl_s: float = Field(86400.0, gt=0)
    approval_then: Literal["promote", "canary"] = "promote"

    @field_validator("canary_steps")
    @classmethod
    def _steps(cls, steps: list[int]) -> list[int]:
        if not steps or steps[-1] != 100:
            raise ValueError("canary_steps must end at 100")
        if any(not 1 <= s <= 100 for s in steps) or steps != sorted(set(steps)):
            raise ValueError("canary_steps must be strictly increasing percentages in 1..100")
        return steps


def policy_hash(policy: BaseModel) -> str:
    canonical = json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_policy_file(path: str, model: type[BaseModel], key: str) -> Any:
    """Read a TOML or JSON policy file (by extension; anything else is tried as TOML)."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"{key.upper()} could not be read", key=key, path=path,
                                 cause=str(exc)) from exc
    try:
        data = json.loads(raw) if path.lower().endswith(".json") else tomllib.loads(raw.decode())
        return model.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(f"{key.upper()} is not a valid policy", key=key, path=path,
                                 cause=str(exc)) from exc
