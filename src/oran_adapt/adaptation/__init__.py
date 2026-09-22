"""Member 3 - adaptation core: inspect a real model artifact, load it, assess what it can do,
and pick the engine that would carry out an approved strategy."""

from oran_adapt.adaptation.capability import assess_capability, assess_schema_compatibility
from oran_adapt.adaptation.data import (
    build_training_frame,
    load_data_version_frame,
    split_features_target,
)
from oran_adapt.adaptation.engines import EngineKind, run_engine, select_engine
from oran_adapt.adaptation.finetune import fine_tune_sklearn
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.llm_adapter import adapt_via_llm
from oran_adapt.adaptation.loaders import load_native_model
from oran_adapt.adaptation.retrain import full_retrain
from oran_adapt.adaptation.schemas import (
    CandidateModel,
    CapabilityAssessment,
    ModelInspection,
    SchemaCompatibility,
)
from oran_adapt.adaptation.torch_engine import fine_tune_torch, full_retrain_torch

__all__ = [
    "CandidateModel",
    "CapabilityAssessment",
    "EngineKind",
    "ModelInspection",
    "SchemaCompatibility",
    "adapt_via_llm",
    "assess_capability",
    "assess_schema_compatibility",
    "build_training_frame",
    "fine_tune_sklearn",
    "fine_tune_torch",
    "full_retrain",
    "full_retrain_torch",
    "inspect_model",
    "load_data_version_frame",
    "load_native_model",
    "run_engine",
    "select_engine",
    "split_features_target",
]
