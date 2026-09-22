"""The orchestrator: wires analysis (Member 1), decision (Member 2), adaptation (Member 3, with
its LLM/sandbox fallback) and validation (Member 4) into the single pipeline that turns one
drift event into either "no action", a rejected candidate, or a newly registered model."""

from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.orchestrator.schemas import JobResult

__all__ = ["JobResult", "run_adaptation_job"]
