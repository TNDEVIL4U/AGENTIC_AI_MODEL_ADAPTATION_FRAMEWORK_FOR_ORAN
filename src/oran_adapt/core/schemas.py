"""Pydantic contracts for the O-RAN facing API (Level-1 integration)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oran_adapt.core.enums import JobStatus


class DriftEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    feature: str | None = None
    statistic: float | None = None
    p_value: float | None = None


class DriftEvent(BaseModel):
    """Inbound drift notification. Member 1 consumes it; nobody re-detects drift."""

    model_id: str = Field(min_length=1)
    model_type: str | None = None
    drift_detected: bool
    drift_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: DriftEvidence = Field(default_factory=DriftEvidence)
    event_id: str | None = Field(
        default=None, description="Caller-supplied id; used for idempotency when present."
    )
    dataset_id: str | None = None
    drifted_data_version: str | None = None
    detected_at: datetime | None = None

    def idempotency_key(self) -> str:
        if self.event_id:
            return f"{self.model_id}:{self.event_id}"
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
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
