"""Member 4 - validation engine: scores V_current and a candidate on the same held-out
validation set and asks the validation gate (validation.gate) whether the candidate may replace
it. This is the single gate between "an engine or the LLM produced a model" and "a model gets
registered and goes live" - nothing downstream trusts a candidate this hasn't approved.

The verdict is a statistical one under the configured GatePolicy (GATE_POLICY): a paired
bootstrap on the task's primary metric plus guardrails. The full decision - thresholds, interval,
policy version and hash, every guardrail - travels in ValidationReport.gate and is stored as a
gate_decision row by the pipeline.
"""

from __future__ import annotations

import joblib
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import TaskType
from oran_adapt.core.errors import ArtifactError, ValidationFailedError
from oran_adapt.validation.evaluate import predict, score_predictions, task_of
from oran_adapt.validation.gate import Scored, decide
from oran_adapt.validation.metrics import primary_metric
from oran_adapt.validation.schemas import ValidationReport


def _scored(model: object, X: pd.DataFrame, y: pd.Series | None, *, framework: str,
            estimator_type: str, task: TaskType) -> Scored:
    predictions, score = predict(model, X, y, framework=framework,
                                 estimator_type=estimator_type, task=task)
    metrics = score_predictions(task, X, y, predictions, score, estimator_type=estimator_type)

    def run(frame: pd.DataFrame) -> object:
        return predict(model, frame, None, framework=framework, estimator_type=estimator_type,
                       task=task)

    return Scored(model=model, predictions=predictions, score=score, metrics=metrics, run=run)


def validate_candidate(
    candidate: CandidateModel,
    current_model: object,
    *,
    model_id: str,
    X: pd.DataFrame,
    y: pd.Series,
    current_framework: str,
    estimator_type: str,
    settings: Settings,
    task_type: str | None = None,
    context: pd.DataFrame | None = None,
) -> ValidationReport:
    """Raises ValidationFailedError if there isn't enough held-out data to score at all, or if
    either model fails to produce predictions on it. Otherwise always returns a ValidationReport
    - the verdict lives in its ``passed`` field (and ``gate``), not in whether this raises.
    ``context`` is the held-out frame, row-aligned with ``X``, that slice guardrails read."""
    if len(X) < settings.validation_min_rows:
        raise ValidationFailedError(
            f"only {len(X)} validation rows available, need >= {settings.validation_min_rows}"
        )

    try:
        candidate_model = joblib.load(candidate.artifact_path)
    except Exception as exc:
        raise ArtifactError(
            f"could not load candidate artifact for validation: {candidate.artifact_path}",
            cause=str(exc),
        ) from exc

    task = task_of(task_type, estimator_type)
    current = _scored(current_model, X, y, framework=current_framework,
                      estimator_type=estimator_type, task=task)
    challenger = _scored(candidate_model, X, y, framework=candidate.framework,
                         estimator_type=estimator_type, task=task)
    metric_name = primary_metric(task)
    decision = decide(settings.gate_policy, task=task, metric=metric_name, X=X, y=y,
                      current=current, candidate=challenger, context=context)

    return ValidationReport(
        model_id=model_id,
        metric_name=metric_name,
        current_metrics=current.metrics,
        candidate_metrics=challenger.metrics,
        current_value=decision.current_value,
        candidate_value=decision.candidate_value,
        threshold=decision.threshold,
        passed=decision.verdict == "ACCEPT",
        reason=" | ".join(decision.reasons),
        n_validation_rows=len(X),
        gate=decision,
    )
