"""The orchestrator: the only place that calls all four members, in sequence, for one drift
event. Each member stays ignorant of the others - this module is the seam.

    analyze() [Member 1] -> decide() [Member 2] -> select_engine()/run_engine() [Member 3],
    falling back to adapt_via_llm() (sandboxed) when the engine registry has no built-in engine
    for the chosen (strategy, framework) -> validate_candidate() [Member 4] -> registry
    (MLflow): register the candidate and move the live alias to it, but only when validation
    passed.

Nothing here mutates the live model unless validate_candidate() says the candidate is good
enough. Job persistence, idempotency and retries (AdaptationJob rows) are Phase 10's concern;
this module is the pure pipeline they will wrap.
"""

from __future__ import annotations

import os

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.adaptation.capability import assess_capability
from oran_adapt.adaptation.data import build_training_frame, split_features_target
from oran_adapt.adaptation.engines import run_engine, select_engine
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.llm_adapter import adapt_via_llm
from oran_adapt.adaptation.loaders import load_native_model
from oran_adapt.adaptation.schemas import CandidateModel, ModelInspection
from oran_adapt.analysis.engine import analyze
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.models import ModelMetadata
from oran_adapt.decision.engine import decide
from oran_adapt.llm.client import LlmClient
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.validation.engine import validate_candidate

_ADAPTING_STRATEGIES = (Strategy.FINE_TUNING, Strategy.FULL_RETRAINING)


def _produce_candidate(
    decision_strategy: Strategy,
    current_model: object,
    *,
    inspection: ModelInspection,
    framework: str,
    X: pd.DataFrame,
    y: pd.Series,
    target_column: str,
    settings: Settings,
    llm_client: LlmClient | None,
    workdir: str,
) -> CandidateModel:
    """Try the built-in engine registry first; only fall back to the LLM sandbox adapter when
    the registry genuinely has nothing for this (strategy, framework, capability) combination."""
    capability = assess_capability(inspection, list(X.columns))
    try:
        engine = select_engine(decision_strategy, framework, capability)
        return run_engine(
            engine,
            current_model,
            inspection=inspection,
            X=X,
            y=y,
            target_column=target_column,
            artifact_dir=os.path.join(workdir, "engine"),
        )
    except UnsupportedAdaptationError:
        if llm_client is None:
            raise
        return adapt_via_llm(
            llm_client,
            current_model,
            framework=framework,
            model_class=inspection.model_class,
            X=X,
            y=y,
            target_column=target_column,
            sandbox_timeout_s=settings.sandbox_timeout_s,
            sandbox_memory_mb=settings.sandbox_memory_mb,
            workdir=os.path.join(workdir, "llm"),
            sandbox_backend=settings.sandbox_backend,
            sandbox_docker_image=settings.sandbox_docker_image,
        )


def run_adaptation_job(
    session: Session,
    event: DriftEvent,
    settings: Settings,
    *,
    registry: MlflowRegistry,
    llm_client: LlmClient | None,
    workdir: str,
) -> JobResult:
    """Run the full pipeline for one drift event. Raises ModelNotFoundError if
    ``event.model_id`` isn't registered (Member 1's precondition) or ArtifactError if the model
    is adaptable but has no ``target_column`` on record. Every other outcome - reuse, no
    compatible strategy, a rejected candidate, a registered candidate - comes back as a
    JobResult, never an exception."""
    analysis = analyze(session, event, settings)

    if analysis.status in ("REUSE", "INSUFFICIENT_DATA"):
        return JobResult(model_id=event.model_id, outcome="NO_ACTION", reason=analysis.reason)

    package = analysis.decision_package
    assert package is not None  # PACKAGED always carries one

    decision = decide(package, settings, llm_client)

    if decision.strategy not in _ADAPTING_STRATEGIES:
        return JobResult(
            model_id=event.model_id,
            outcome="NO_ACTION",
            strategy=decision.strategy,
            decision=decision,
            reason=decision.rationale,
        )

    model_meta = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == event.model_id)
    ).scalar_one()
    if not model_meta.target_column:
        raise ArtifactError(
            f"model '{event.model_id}' has no target_column on record; cannot train or validate",
            model_id=event.model_id,
        )

    live_version = registry.get_version_by_alias(model_meta.mlflow_model_name, settings.live_alias)
    local_path = registry.download_artifacts(
        model_meta.mlflow_model_name, live_version, os.path.join(workdir, "current")
    )
    current_model = load_native_model(local_path, model_meta.framework)
    inspection = inspect_model(current_model, model_meta.framework)

    train_ids = [
        ref.data_version_id for ref in (package.historical_data, package.drifted_data) if ref
    ]
    train_frame = build_training_frame(session, train_ids)
    feature_names = inspection.feature_names_in or [
        c for c in train_frame.columns if c != model_meta.target_column
    ]
    X, y = split_features_target(train_frame, feature_names, model_meta.target_column)

    candidate = _produce_candidate(
        decision.strategy,
        current_model,
        inspection=inspection,
        framework=model_meta.framework,
        X=X,
        y=y,
        target_column=model_meta.target_column,
        settings=settings,
        llm_client=llm_client,
        workdir=workdir,
    )

    validation_ids = (
        [package.drifted_data.data_version_id] if package.drifted_data else train_ids
    )
    validation_frame = build_training_frame(session, validation_ids)
    Xv, yv = split_features_target(validation_frame, feature_names, model_meta.target_column)
    report = validate_candidate(
        candidate,
        current_model,
        model_id=event.model_id,
        X=Xv,
        y=yv,
        current_framework=model_meta.framework,
        estimator_type=inspection.estimator_type,
        settings=settings,
    )

    if not report.passed:
        return JobResult(
            model_id=event.model_id,
            outcome="REJECTED",
            strategy=decision.strategy,
            decision=decision,
            candidate=candidate,
            validation=report,
            reason=report.reason,
        )

    new_version = registry.register_candidate(
        model_meta.mlflow_model_name,
        candidate.artifact_path,
        framework=candidate.framework,
        metrics=candidate.metrics,
    )
    registry.set_alias(model_meta.mlflow_model_name, settings.live_alias, new_version)

    return JobResult(
        model_id=event.model_id,
        outcome="REGISTERED",
        strategy=decision.strategy,
        decision=decision,
        candidate=candidate,
        validation=report,
        registered_version=new_version,
        reason=f"candidate validated ({report.reason}) and registered as version {new_version}",
    )
