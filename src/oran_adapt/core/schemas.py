"""Pydantic contracts for the O-RAN facing API (Level-1 integration)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oran_adapt.core.enums import JobStatus


class DriftEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    feature: str | None = None
    statistic: float | None = None
    p_value: float | None = None


DriftType = Literal["feature", "prediction", "concept", "data_quality"]
Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]

# Fields added in Phase 14 for the Team 1 contract. When they are all unset, the idempotency key
# is computed exactly as before, so an event resent from before the upgrade is still a duplicate.
_TEAM1_FIELDS = (
    "model_version",
    "timestamp",
    "drift_type",
    "severity",
    "drift_metrics",
    "affected_features",
    "source_dataset_version",
    "data_start_time",
    "data_end_time",
)


class DriftEvent(BaseModel):
    """Inbound drift notification from Team 1. Member 1 re-checks the drift against the data
    before anything is adapted; the event alone never changes a model."""

    model_id: str = Field(min_length=1, max_length=200)
    model_type: str | None = None
    # Team 1 sends events when it sees drift, so a missing flag means drift was detected.
    drift_detected: bool = True
    drift_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: DriftEvidence = Field(default_factory=DriftEvidence)
    event_id: str | None = Field(
        default=None,
        max_length=128,
        description="Caller-supplied id; used for idempotency when present.",
    )
    dataset_id: str | None = None
    drifted_data_version: str | None = None
    detected_at: datetime | None = None

    model_version: str | None = Field(
        default=None,
        max_length=50,
        description="The model version Team 1 observed drifting. If LIVE has since moved to "
        "another version, the event is stale and nothing is adapted.",
    )
    timestamp: datetime | None = None
    drift_type: DriftType | None = None
    severity: Severity | None = None
    drift_metrics: dict[str, float] = Field(default_factory=dict)
    affected_features: list[str] = Field(default_factory=list)
    source_dataset_version: str | None = None
    data_start_time: datetime | None = None
    data_end_time: datetime | None = None

    @model_validator(mode="after")
    def _time_range_ordered(self) -> DriftEvent:
        if (
            self.data_start_time is not None
            and self.data_end_time is not None
            and self.data_end_time < self.data_start_time
        ):
            raise ValueError("data_end_time must not be before data_start_time")
        return self

    def idempotency_key(self) -> str:
        if self.event_id:
            return f"{self.model_id}:{self.event_id}"
        body = self.model_dump(mode="json")
        for name in _TEAM1_FIELDS:
            if body.get(name) in (None, [], {}):
                body.pop(name, None)
        canonical = json.dumps(body, sort_keys=True)
        return f"{self.model_id}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


class JobResponse(BaseModel):
    job_id: str
    model_id: str
    status: JobStatus
    strategy: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    duplicate: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ComponentHealth(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadyResponse(BaseModel):
    ready: bool
    components: list[ComponentHealth]
