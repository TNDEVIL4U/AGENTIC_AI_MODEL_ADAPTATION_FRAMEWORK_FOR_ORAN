"""The orchestrator: the only place that calls all four members, in sequence, for one drift
event. Each member stays ignorant of the others - this module is the seam.

    analyze() [Member 1] -> evaluate_versions() + decide_reuse() [Member 1]: score every
    registered version on the current data and, if one clearly beats LIVE, promote it and stop
    (no training) -> decide() [Member 2] -> select_engine()/run_engine() [Member 3], falling
    back to adapt_via_llm() (sandboxed) when the engine registry has no built-in engine for the
    chosen (strategy, framework) -> validate_candidate() [Member 4] -> registry (MLflow):
    register the candidate, then promote_version() moves the live alias to it, but only when
    validation passed.

Nothing here mutates the live model except through registry.promotion, which records the
previous LIVE and undoes a half-finished move. Job persistence, idempotency, locking and
retries are orchestrator.jobs' concern; each stage entered here is reported to it through
orchestrator.context.
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
from oran_adapt.analysis.reuse_decision import decide_reuse
from oran_adapt.analysis.schemas import ReuseDecision, VersionEvaluation
from oran_adapt.analysis.version_eval import evaluate_versions
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import (
    JobStatus,
    ModelVersionStatus,
    PromotionKind,
    ReuseVerdict,
    Strategy,
)
from oran_adapt.core.errors import ArtifactError, PromotionError, UnsupportedAdaptationError
from oran_adapt.core.integrity import sha256_file, verify_checksum
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.datastore.versioning import snapshot_training_data
from oran_adapt.db.models import ModelMetadata, ModelVersionEvaluation
from oran_adapt.decision.engine import decide
from oran_adapt.llm.client import LlmClient
from oran_adapt.orchestrator.context import current_job_id, report_stage
from oran_adapt.orchestrator.schemas import JobResult
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.registry.promotion import current_live, promote_version, verify_version_artifact
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


def _stage(session: Session, status: JobStatus, message: str = "") -> None:
    """Commit what the previous stage wrote, then record the new stage. Committing first keeps
    the job's transition (its own short transaction) from waiting on this session's writes."""
    session.commit()
    report_stage(status, message)


def _record_evaluations(
    session: Session, model_id: str, evaluations: list[VersionEvaluation], reuse: ReuseDecision
) -> None:
    job_id = current_job_id()
    for ev in evaluations:
        chosen = ev.version == reuse.selected_version
        session.add(
            ModelVersionEvaluation(
                job_id=job_id,
                model_id=model_id,
                model_version=ev.version,
                is_live=ev.is_live,
                compatible=ev.compatible,
                reusable=chosen,
                metric_name=ev.metric_name,
                metric_value=ev.metric_value,
                reuse_score=reuse.improvement if chosen else None,
                n_rows=ev.n_rows,
                result=ev.model_dump(mode="json"),
            )
        )
    session.flush()


def _promotion_key(suffix: str) -> str | None:
    job_id = current_job_id()
    return f"{job_id}:{suffix}" if job_id else None


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
    is adaptable but has no ``target_column`` on record. Every other outcome - no action, an
    existing version reused, a rejected candidate, a registered candidate - comes back as a
    JobResult, never an exception."""
    model_meta = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == event.model_id)
    ).scalar_one_or_none()
    live_version = (
        current_live(registry, model_meta.mlflow_model_name, settings.live_alias)
        if model_meta is not None
        else None
    )

    if (
        event.model_version is not None
        and live_version is not None
        and event.model_version != live_version
    ):
        return JobResult(
            model_id=event.model_id,
            outcome="NO_ACTION",
            live_version=live_version,
            reason=(
                f"stale event: drift was reported on version {event.model_version} but LIVE is "
                f"now version {live_version}"
            ),
        )

    analysis = analyze(session, event, settings, live_version=live_version)
    assert model_meta is not None  # analyze() raised ModelNotFoundError otherwise

    if analysis.status in ("REUSE", "INSUFFICIENT_DATA"):
        return JobResult(
            model_id=event.model_id,
            outcome="NO_ACTION",
            live_version=live_version,
            reason=analysis.reason,
        )

    package = analysis.decision_package
    assert package is not None  # PACKAGED always carries one
    target = model_meta.target_column

    # CurrentData: hold out the newest drifted rows (all sources when there is no drifted
    # version). Every registered version is scored on them, the candidate never trains on
    # them, and the validation gate scores the candidate on them too.
    train_ids = [
        ref.data_version_id for ref in (package.historical_data, package.drifted_data) if ref
    ]
    records = load_records(session, train_ids)
    pool_id = package.drifted_data.data_version_id if package.drifted_data else None
    pool = [r for r in records if pool_id is None or r.data_version_id == pool_id]
    n_holdout = holdout_size(
        len(pool), settings.validation_holdout_fraction, settings.validation_min_rows
    )
    holdout = pool[len(pool) - n_holdout :]
    holdout_ids = {r.id for r in holdout}
    holdout_frame = records_frame(holdout)

    evaluations: list[VersionEvaluation] = []
    reuse: ReuseDecision | None = None
    can_evaluate = (
        settings.reuse_enabled
        and target is not None
        and live_version is not None
        and len(holdout_frame) >= settings.validation_min_rows
        and target in holdout_frame.columns
    )
    if can_evaluate:
        _stage(session, JobStatus.EVALUATING_VERSIONS, "scoring registered versions")
        evaluations = evaluate_versions(
            registry,
            mlflow_name=model_meta.mlflow_model_name,
            framework=model_meta.framework,
            target_column=target,
            live_version=live_version,
            data=holdout_frame,
            settings=settings,
            workdir=workdir,
        )
        reuse = decide_reuse(
            evaluations, live_version=live_version, max_psi=package.max_psi, settings=settings
        )
        _record_evaluations(session, event.model_id, evaluations, reuse)
        _stage(session, JobStatus.REUSE_DECISION, reuse.reason)

        if reuse.verdict == ReuseVerdict.REUSE_EXISTING_VERSION:
            assert reuse.selected_version is not None
            _stage(session, JobStatus.PROMOTING, f"reusing version {reuse.selected_version}")
            promotion = promote_version(
                session,
                registry,
                model_id=event.model_id,
                version=reuse.selected_version,
                kind=PromotionKind.REUSE,
                live_alias=settings.live_alias,
                workdir=os.path.join(workdir, "promote"),
                reason=reuse.reason,
                job_id=current_job_id(),
                expected_live=live_version,
                idempotency_key=_promotion_key("reuse"),
            )
            return JobResult(
                model_id=event.model_id,
                outcome="REUSED",
                reused_version=reuse.selected_version,
                previous_live_version=live_version,
                live_version=reuse.selected_version,
                version_evaluations=evaluations,
                reuse_decision=reuse,
                promotion=promotion.to_dict(),
                reason=reuse.reason,
            )

    _stage(session, JobStatus.DECISION_PENDING, "handing the package to the decision engine")
    package = package.model_copy(
        update={"version_evaluations": evaluations, "reuse_decision": reuse}
    )
    decision = decide(package, settings, llm_client)

    if decision.strategy not in _ADAPTING_STRATEGIES:
        return JobResult(
            model_id=event.model_id,
            outcome="NO_ACTION",
            strategy=decision.strategy,
            decision=decision,
            live_version=live_version,
            version_evaluations=evaluations,
            reuse_decision=reuse,
            reason=decision.rationale,
        )

    if not target:
        raise ArtifactError(
            f"model '{event.model_id}' has no target_column on record; cannot train or validate",
            model_id=event.model_id,
        )
    if live_version is None:  # raises ModelNotFoundError: nothing live to adapt from
        live_version = registry.get_version_by_alias(
            model_meta.mlflow_model_name, settings.live_alias
        )

    _stage(session, JobStatus.ADAPTING, f"{decision.strategy.value} from version {live_version}")
    local_path, _ = verify_version_artifact(
        registry, model_meta.mlflow_model_name, live_version, os.path.join(workdir, "current")
    )
    current_model = load_native_model(local_path, model_meta.framework)
    inspection = inspect_model(current_model, model_meta.framework)

    train_frame = records_frame([r for r in records if r.id not in holdout_ids])
    feature_names = inspection.feature_names_in or [c for c in train_frame.columns if c != target]
    X, y = split_features_target(train_frame, feature_names, target)

    candidate = _produce_candidate(
        decision.strategy,
        current_model,
        inspection=inspection,
        framework=model_meta.framework,
        X=X,
        y=y,
        target_column=target,
        settings=settings,
        llm_client=llm_client,
        workdir=workdir,
    )
    candidate_sha = sha256_file(candidate.artifact_path)

    _stage(session, JobStatus.VALIDATING_CANDIDATE, "scoring the candidate on held-out data")
    verify_checksum(candidate.artifact_path, candidate_sha, stage="validation")
    validation_frame = holdout_frame
    if validation_frame.empty:  # validate_candidate then reports "not enough validation rows"
        validation_frame = pd.DataFrame(columns=[*feature_names, target])
    Xv, yv = split_features_target(validation_frame, feature_names, target)
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
            live_version=live_version,
            version_evaluations=evaluations,
            reuse_decision=reuse,
            reason=report.reason,
        )

    _stage(session, JobStatus.REGISTERING, "registering the validated candidate")
    verify_checksum(candidate.artifact_path, candidate_sha, stage="registration")
    source_versions = [
        ref for ref in (package.historical_data, package.drifted_data) if ref is not None
    ]
    name = model_meta.mlflow_model_name
    new_version = registry.register_candidate(
        name,
        candidate.artifact_path,
        framework=candidate.framework,
        metrics=candidate.metrics,
        tags={
            "oran.model_id": event.model_id,
            "oran.parent_version": live_version,
            "oran.event_id": event.event_id or "",
            "oran.job_id": current_job_id() or "",
            "oran.status": ModelVersionStatus.VALIDATED.value,
            "adaptation.strategy": decision.strategy.value,
            "adaptation.engine": candidate.engine.value,
            "candidate.sha256": candidate_sha,
            "validation.metric": report.metric_name,
            "validation.candidate_value": f"{report.candidate_value:.6f}",
            "validation.current_value": f"{report.current_value:.6f}",
            "validation.holdout_rows": str(len(holdout)),
            "data.source_versions": ",".join(ref.version for ref in source_versions),
        },
    )
    registry.record_artifact_checksum(name, new_version, os.path.join(workdir, "registered"))
    registry.set_alias(name, settings.candidate_alias, new_version)
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
        name,
        new_version,
        {
            "data.training_version": snapshot.version,
            "data.training_hash": snapshot.content_hash or "",
        },
    )

    _stage(session, JobStatus.PROMOTING, f"moving LIVE from {live_version} to {new_version}")
    result = JobResult(
        model_id=event.model_id,
        outcome="REGISTERED",
        strategy=decision.strategy,
        decision=decision,
        candidate=candidate,
        validation=report,
        registered_version=new_version,
        training_data_version=snapshot.version,
        previous_live_version=live_version,
        live_version=new_version,
        version_evaluations=evaluations,
        reuse_decision=reuse,
        reason=f"candidate validated ({report.reason}) and registered as version {new_version}",
    )
    try:
        promotion = promote_version(
            session,
            registry,
            model_id=event.model_id,
            version=new_version,
            kind=PromotionKind.PROMOTE_CANDIDATE,
            live_alias=settings.live_alias,
            workdir=os.path.join(workdir, "promote"),
            reason=result.reason,
            job_id=current_job_id(),
            expected_live=live_version,
            idempotency_key=_promotion_key("promote"),
        )
    except PromotionError as exc:
        return result.model_copy(
            update={
                "outcome": "ROLLED_BACK",
                "live_version": live_version,
                "previous_live_version": None,
                "reason": f"version {new_version} registered but not promoted: {exc.message}",
            }
        )
    return result.model_copy(update={"promotion": promotion.to_dict()})
