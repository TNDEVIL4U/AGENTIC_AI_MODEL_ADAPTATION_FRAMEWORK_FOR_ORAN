"""Pydantic contracts internal to Member 3 (adaptation): what the inspector and capability
assessor hand each other and, eventually, the orchestrator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oran_adapt.core.enums import EngineKind, Strategy


class ModelInspection(BaseModel):
    """What we learned by loading the real model artifact and looking at it directly - never
    guessed from metadata alone."""

    framework: str
    model_class: str
    estimator_type: Literal[
        "classifier", "regressor", "clusterer", "outlier_detector", "unknown"
    ] = "unknown"
    n_features_in: int | None = None
    feature_names_in: list[str] | None = None
    supports_partial_fit: bool = False
    supports_warm_start: bool = False
    n_parameters: int | None = None
    input_dim: int | None = None
    output_dim: int | None = None
    # Transforms applied before the final estimator (an sklearn Pipeline's earlier steps): the
    # model expects raw features and does its own preprocessing.
    preprocessing_steps: list[str] = Field(default_factory=list)
    classes: list[str] | None = None  # a classifier's labels, as strings


class SchemaCompatibility(BaseModel):
    compatible: bool
    missing_features: list[str] = Field(default_factory=list)
    extra_features: list[str] = Field(default_factory=list)
    reason: str


class CapabilityAssessment(BaseModel):
    supports_fine_tuning: bool
    supports_full_retraining: bool
    schema_check: SchemaCompatibility
    reason: str


class CandidateModel(BaseModel):
    """A real, fitted model produced by an engine and saved to a local artifact path - not yet
    validated or registered."""

    engine: EngineKind
    framework: str
    model_class: str
    artifact_path: str
    metrics: dict[str, float] = Field(default_factory=dict)
    n_train_rows: int
    feature_names: list[str]
    target_column: str
    # What was actually carried out, when it differs from the decision (a FINE_TUNING decision
    # the artifact cannot support natively runs as FULL_RETRAINING), and why.
    applied_strategy: Strategy | None = None
    adaptation_note: str = ""
