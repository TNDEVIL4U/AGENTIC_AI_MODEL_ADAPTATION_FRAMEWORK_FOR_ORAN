"""Member 4 - validation engine: scores V_current and a candidate on the same held-out
validation set and decides whether the candidate is good enough to register. This is the single
gate between "an engine or the LLM produced a model" and "a model gets registered and goes
live" - nothing downstream trusts a candidate this hasn't approved. Both models are compared on
the task's primary metric (validation.metrics). For a higher-is-better metric (accuracy, F1,
silhouette) the candidate passes when it is no more than validation_accuracy_tolerance below
current's; for an error metric (RMSE) when it is no more than validation_rmse_tolerance_ratio
(a fraction of current's value) above it.
"""

from __future__ import annotations

import joblib
import pandas as pd

from oran_adapt.adaptation.schemas import CandidateModel
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import ArtifactError, ValidationFailedError
from oran_adapt.validation.evaluate import evaluate_model
from oran_adapt.validation.metrics import higher_is_better
from oran_adapt.validation.schemas import ValidationReport


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
) -> ValidationReport:
    """Raises ValidationFailedError if there isn't enough held-out data to score at all, or if
    either model fails to produce predictions on it. Otherwise always returns a ValidationReport
    - the pass/fail verdict lives in its `passed` field, not in whether this raises."""
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

    current_metrics = evaluate_model(
        current_model,
        X,
        y,
        framework=current_framework,
        estimator_type=estimator_type,
        task_type=task_type,
    )
    candidate_metrics = evaluate_model(
        candidate_model,
        X,
        y,
        framework=candidate.framework,
        estimator_type=estimator_type,
        task_type=task_type,
    )

    metric_name = next(iter(current_metrics))
    current_value = current_metrics[metric_name]
    candidate_value = candidate_metrics[metric_name]

    if higher_is_better(metric_name):
        threshold = settings.validation_accuracy_tolerance
        passed = candidate_value >= current_value - threshold
    else:  # an error metric - lower is better, tolerance scales with current's own value
        threshold = current_value * settings.validation_rmse_tolerance_ratio
        passed = candidate_value <= current_value + threshold

    reason = (
        f"candidate {metric_name}={candidate_value:.4f} vs current {metric_name}="
        f"{current_value:.4f} (tolerance {threshold:.4f}) -> {'PASS' if passed else 'FAIL'}"
    )

    return ValidationReport(
        model_id=model_id,
        metric_name=metric_name,
        current_metrics=current_metrics,
        candidate_metrics=candidate_metrics,
        current_value=current_value,
        candidate_value=candidate_value,
        threshold=threshold,
        passed=passed,
        reason=reason,
        n_validation_rows=len(X),
    )
