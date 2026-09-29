"""What the orchestrator hands back for one drift event: which of the four members ran, what
each produced, and how the job ended."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oran_adapt.adaptation.leakage import LeakageReport
from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.analysis.schemas import ReuseDecision, VersionEvaluation
from oran_adapt.core.enums import Strategy
from oran_adapt.decision.schemas import Decision
from oran_adapt.validation.schemas import ValidationReport


class JobResult(BaseModel):
    model_id: str
    # REUSED: an existing version went live, nothing trained. ROLLED_BACK: a validated
    # candidate was registered but moving LIVE to it failed, and LIVE was restored.
    # DELIVERING: a validated candidate was registered and its progressive rollout started
    # (``rollout``); LIVE moves only when the rollout promotes it. ROLLOUT_IN_PROGRESS: the
    # model already has a rollout running, so this event started no adaptation.
    outcome: Literal[
        "NO_ACTION", "REUSED", "REGISTERED", "REJECTED", "ROLLED_BACK", "DELIVERING",
        "ROLLOUT_IN_PROGRESS",
    ]
    reason: str
    strategy: Strategy | None = None
    decision: Decision | None = None
    candidate: CandidateModel | None = None
    validation: ValidationReport | None = None
    registered_version: str | None = None
    # The data version the pipeline froze as the new model version's training set.
    training_data_version: str | None = None
    reused_version: str | None = None
    # What LIVE pointed at before this job moved it (None when LIVE did not move).
    previous_live_version: str | None = None
    live_version: str | None = None
    # The CurrentData (cleaned, versioned evaluation rows) this job decided on.
    current_data_id: str | None = None
    version_evaluations: list[VersionEvaluation] = Field(default_factory=list)
    reuse_decision: ReuseDecision | None = None
    # What the leakage checks found and removed before training (None when nothing trained).
    leakage: LeakageReport | None = None
    promotion: dict | None = None
    # The rollout this job started (DELIVERING) or found running (ROLLOUT_IN_PROGRESS).
    rollout: dict | None = None
