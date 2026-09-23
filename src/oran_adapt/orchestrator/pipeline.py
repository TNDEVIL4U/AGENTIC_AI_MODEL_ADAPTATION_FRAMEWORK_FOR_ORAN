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
from oran_adapt.adaptation.data import (
    holdout_size,
    load_records,
    records_frame,
    split_features_target,
)
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
from oran_adapt.datastore.versioning import snapshot_training_data
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
            torch_fine_tune_epochs=settings.torch_fine_tune_epochs,
            torch_full_retrain_epochs=settings.torch_full_retrain_epochs,
            torch_learning_rate=settings.torch_learning_rate,
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
    # Hold out the newest drifted rows (all sources when there is no drifted version): both
    # models are scored on them, and the candidate never trains on them.
    records = load_records(session, train_ids)
    pool_id = package.drifted_data.data_version_id if package.drifted_data else None
    pool = [r for r in records if pool_id is None or r.data_version_id == pool_id]
    n_holdout = holdout_size(
        len(pool), settings.validation_holdout_fraction, settings.validation_min_rows
    )
    holdout = pool[len(pool) - n_holdout :]
    holdout_ids = {r.id for r in holdout}
    train_frame = records_frame([r for r in records if r.id not in holdout_ids])
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

    validation_frame = records_frame(holdout)
    if validation_frame.empty:  # validate_candidate then reports "not enough validation rows"
        validation_frame = pd.DataFrame(columns=[*feature_names, model_meta.target_column])
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

    source_versions = [
        ref for ref in (package.historical_data, package.drifted_data) if ref is not None
    ]
    new_version = registry.register_candidate(
        model_meta.mlflow_model_name,
        candidate.artifact_path,
        framework=candidate.framework,
        metrics=candidate.metrics,
        tags={
            "oran.model_id": event.model_id,
            "oran.parent_version": live_version,
            "oran.event_id": event.event_id or "",
            "adaptation.strategy": decision.strategy.value,
            "adaptation.engine": candidate.engine.value,
            "validation.metric": report.metric_name,
            "validation.candidate_value": f"{report.candidate_value:.6f}",
            "validation.current_value": f"{report.current_value:.6f}",
            "validation.holdout_rows": str(len(holdout)),
            "data.source_versions": ",".join(ref.version for ref in source_versions),
        },
    )
    # Data lineage: freeze what v<new> was trained on as its own data version, linked to it, so
    # the next drift event is compared against the live model's real baseline.
    snapshot = snapshot_training_data(
        session,
        model_id=event.model_id,
        model_version=new_version,
        source_version_ids=train_ids,
        exclude_record_ids=holdout_ids,
        parent_version_id=package.historical_data.data_version_id
        if package.historical_data
        else None,
        job_ref=event.event_id,
    )
    registry.set_version_tags(
        model_meta.mlflow_model_name,
        new_version,
        {
            "data.training_version": snapshot.version,
            "data.training_hash": snapshot.content_hash or "",
        },
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
        training_data_version=snapshot.version,
        reason=f"candidate validated ({report.reason}) and registered as version {new_version}",
    )
