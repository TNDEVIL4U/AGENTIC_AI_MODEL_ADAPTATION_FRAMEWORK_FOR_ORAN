"""Model onboarding: the one call that makes a model manageable by the adaptation pipeline.

It registers the model in MLflow (new version + live alias, tagged with its training-data
lineage), records its ModelMetadata, and versions its training data (and optionally an
already-observed drifted slice) in the data store, linked to that exact MLflow version.
Everything the pipeline later needs for a drift event on ``model_id`` is in place afterwards.

Alternatively ``attach_existing_model`` adopts a model that is *already* in MLflow (the usual
case when data scientists publish to a shared registry) without re-uploading it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from oran_adapt.core.enums import AssociationRole, DataKind
from oran_adapt.core.errors import ArtifactError, ConflictError
from oran_adapt.datastore.versioning import (
    DEFAULT_ROW_SPACING,
    VersionInfo,
    get_version,
    ingest_version,
    link_model_data,
)
from oran_adapt.db.models import ModelMetadata
from oran_adapt.registry.client import MlflowRegistry


@dataclass
class OnboardResult:
    model_id: str
    mlflow_model_name: str
    model_version: str
    live_alias: str
    training_data: VersionInfo | None
    drifted_data: VersionInfo | None = None

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "mlflow_model_name": self.mlflow_model_name,
            "model_version": self.model_version,
            "live_alias": self.live_alias,
            "training_data": self.training_data.as_dict() if self.training_data else None,
            "drifted_data": self.drifted_data.as_dict() if self.drifted_data else None,
        }


def _ensure_new_model(session: Session, model_id: str) -> None:
    exists = session.execute(
        select(ModelMetadata).where(ModelMetadata.model_id == model_id)
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError(f"model '{model_id}' is already onboarded", model_id=model_id)


def _check_target(frame: pd.DataFrame, target_column: str) -> None:
    if target_column not in frame.columns:
        raise ArtifactError(
            f"training data has no target column {target_column!r}",
            available_columns=list(frame.columns),
        )


def onboard_model(
    session: Session,
    registry: MlflowRegistry,
    *,
    model_id: str,
    model: object,
    framework: str,
    task_type: str,
    target_column: str,
    dataset_id: str,
    training_frame: pd.DataFrame,
    training_version: str = "v1",
    timestamp_column: str | None = None,
    mlflow_model_name: str | None = None,
    live_alias: str = "live",
    drifted_frame: pd.DataFrame | None = None,
    drifted_version: str | None = None,
) -> OnboardResult:
    """Register ``model`` (an already-fitted native sklearn/xgboost/torch object) and its
    training data. Validation and all DB writes happen before MLflow is touched, so a
    rejected onboarding leaves no orphan registry version behind; the caller commits."""
    _ensure_new_model(session, model_id)
    _check_target(training_frame, target_column)
    name = mlflow_model_name or model_id.replace("-", "_")

    # DB first (inside the caller's still-uncommitted transaction), so the real content hash is
    # known before MLflow is touched; if the MLflow call then fails, the caller's transaction
    # rolls all of this back.
    session.add(
        ModelMetadata(
            model_id=model_id,
            mlflow_model_name=name,
            model_type=task_type,
            framework=framework,
            task_type=task_type,
            target_column=target_column,
        )
    )
    session.flush()
    training = ingest_version(
        session,
        dataset_id,
        training_version,
        training_frame,
        kind=DataKind.HISTORICAL,
        timestamp_column=timestamp_column,
        source="onboarding",
    )

    features = (
        training_frame.drop(columns=[timestamp_column]) if timestamp_column else training_frame
    )
    version = registry.log_model(
        name,
        model,
        framework=framework,
        tags={
            "oran.model_id": model_id,
            "data.dataset_id": dataset_id,
            "data.training_version": training_version,
            "data.training_hash": training.content_hash or "",
        },
        input_frame=features,
        input_name=f"{dataset_id}@{training_version}",
        input_digest=(training.content_hash or "")[:36] or None,
    )
    registry.set_alias(name, live_alias, version)
    link_model_data(
        session, model_id, version, training.data_version_id, AssociationRole.TRAINING
    )

    drifted = None
    if drifted_frame is not None:
        drifted = ingest_version(
            session,
            dataset_id,
            drifted_version or f"{training_version}-drift",
            drifted_frame,
            kind=DataKind.DRIFTED,
            timestamp_column=timestamp_column,
            start=(training.data_end + DEFAULT_ROW_SPACING) if training.data_end else None,
            parent_version=training_version,
            model_id=model_id,
            model_version=version,
            role=AssociationRole.DRIFT_OBSERVED,
            source="onboarding",
        )

    return OnboardResult(
        model_id=model_id,
        mlflow_model_name=name,
        model_version=version,
        live_alias=live_alias,
        training_data=training,
        drifted_data=drifted,
    )


def attach_existing_model(
    session: Session,
    registry: MlflowRegistry,
    *,
    model_id: str,
    mlflow_model_name: str,
    framework: str,
    task_type: str,
    target_column: str,
    live_alias: str = "live",
    version: str | None = None,
    dataset_id: str | None = None,
    training_version: str | None = None,
) -> OnboardResult:
    """Adopt a model that already lives in MLflow. Uses ``version`` (and points ``live_alias``
    at it) or, when omitted, whatever ``live_alias`` already points to. If ``dataset_id`` +
    ``training_version`` name an ingested data version, it is linked as that version's training
    data."""
    _ensure_new_model(session, model_id)
    if version is None:
        version = registry.get_version_by_alias(mlflow_model_name, live_alias)
    else:
        registry.list_versions(mlflow_model_name)  # raises ModelNotFoundError if absent
        registry.set_alias(mlflow_model_name, live_alias, version)

    session.add(
        ModelMetadata(
            model_id=model_id,
            mlflow_model_name=mlflow_model_name,
            model_type=task_type,
            framework=framework,
            task_type=task_type,
            target_column=target_column,
        )
    )
    session.flush()

    training = None
    if dataset_id and training_version:
        training = get_version(session, dataset_id, training_version)
        link_model_data(session, model_id, version, training.data_version_id, AssociationRole.TRAINING)
        registry.set_version_tags(
            mlflow_model_name,
            version,
            {
                "oran.model_id": model_id,
                "data.dataset_id": dataset_id,
                "data.training_version": training_version,
                "data.training_hash": training.content_hash or "",
            },
        )
    return OnboardResult(
        model_id=model_id,
        mlflow_model_name=mlflow_model_name,
        model_version=version,
        live_alias=live_alias,
        training_data=training,
    )
