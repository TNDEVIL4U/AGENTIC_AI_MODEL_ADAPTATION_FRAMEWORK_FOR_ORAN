"""Phase 4: Member 3 core - real sklearn + torch artifacts through loaders, inspector,
capability assessment and engine selection."""

from __future__ import annotations

import mlflow
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from torch import nn

from oran_adapt.adaptation.capability import assess_capability, assess_schema_compatibility
from oran_adapt.adaptation.engines import EngineKind, select_engine
from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.loaders import load_native_model
from oran_adapt.adaptation.schemas import CapabilityAssessment, ModelInspection, SchemaCompatibility
from oran_adapt.core.enums import Strategy
from oran_adapt.core.errors import ArtifactError, ModelNotFoundError, UnsupportedAdaptationError
from oran_adapt.registry.client import MlflowRegistry


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


def _log_sklearn_model(name: str, *, warm_start: bool = False) -> None:
    X = pd.DataFrame({"prb_util": [0.1, 0.2, 0.3, 0.4], "rsrp": [1.0, 2.0, 3.0, 4.0]})
    y = [0, 1, 0, 1]
    clf = LogisticRegression(warm_start=warm_start).fit(X, y)
    with mlflow.start_run():
        mlflow.sklearn.log_model(clf, name="model", registered_model_name=name)


def _log_torch_model(name: str) -> None:
    model = nn.Sequential(nn.Linear(4, 8), nn.ReLU(), nn.Linear(8, 2))
    with mlflow.start_run():
        mlflow.pytorch.log_model(
            model, name="model", registered_model_name=name, serialization_format="pickle"
        )


# ---- loaders + inspector, real artifacts -------------------------------------------------------
def test_sklearn_artifact_round_trip(registry, tmp_path) -> None:
    _log_sklearn_model("sk-test-model", warm_start=True)
    local_path = registry.download_artifacts("sk-test-model", "1", str(tmp_path / "dl"))
    model = load_native_model(local_path, "sklearn")
    inspection = inspect_model(model, "sklearn")

    assert inspection.framework == "sklearn"
    assert inspection.model_class == "LogisticRegression"
    assert inspection.estimator_type == "classifier"
    assert inspection.n_features_in == 2
    assert inspection.feature_names_in == ["prb_util", "rsrp"]
    assert inspection.supports_partial_fit is False
    assert inspection.supports_warm_start is True


def test_torch_artifact_round_trip(registry, tmp_path) -> None:
    _log_torch_model("torch-test-model")
    local_path = registry.download_artifacts("torch-test-model", "1", str(tmp_path / "dl"))
    model = load_native_model(local_path, "torch")
    inspection = inspect_model(model, "torch")

    assert inspection.framework == "torch"
    assert inspection.model_class == "Sequential"
    assert inspection.n_parameters == (4 * 8 + 8) + (8 * 2 + 2)
    assert inspection.input_dim == 4
    assert inspection.output_dim == 2
    assert inspection.supports_partial_fit is False
    assert inspection.supports_warm_start is True


def test_download_missing_version_raises_model_not_found(registry, tmp_path) -> None:
    _log_sklearn_model("sk-test-model-2")
    with pytest.raises(ModelNotFoundError):
        registry.download_artifacts("sk-test-model-2", "999", str(tmp_path / "dl"))


def test_load_native_model_unsupported_framework_raises(tmp_path) -> None:
    with pytest.raises(UnsupportedAdaptationError):
        load_native_model(str(tmp_path), "tensorflow")


def test_load_native_model_wraps_corrupt_artifact_as_artifact_error(tmp_path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ArtifactError):
        load_native_model(str(empty_dir), "sklearn")


def test_inspect_model_unsupported_framework_raises() -> None:
    with pytest.raises(UnsupportedAdaptationError):
        inspect_model(object(), "tensorflow")


# ---- capability assessment (constructed inspections, no I/O) ---------------------------------
def _inspection(**overrides) -> ModelInspection:
    defaults = {
        "framework": "sklearn",
        "model_class": "LogisticRegression",
        "estimator_type": "classifier",
        "n_features_in": 2,
        "feature_names_in": ["prb_util", "rsrp"],
        "supports_partial_fit": False,
        "supports_warm_start": False,
    }
    defaults.update(overrides)
    return ModelInspection(**defaults)


def test_schema_compatible_when_all_features_present() -> None:
    result = assess_schema_compatibility(_inspection(), ["prb_util", "rsrp", "extra"])
    assert result.compatible is True
    assert result.extra_features == ["extra"]
    assert result.missing_features == []


def test_schema_incompatible_when_features_missing() -> None:
    result = assess_schema_compatibility(_inspection(), ["prb_util"])
    assert result.compatible is False
    assert result.missing_features == ["rsrp"]


def test_schema_skipped_when_model_exposes_no_feature_names() -> None:
    result = assess_schema_compatibility(_inspection(feature_names_in=None), ["anything"])
    assert result.compatible is True
    assert "skipped" in result.reason


def test_capability_supports_fine_tuning_when_warm_start() -> None:
    assessment = assess_capability(_inspection(supports_warm_start=True), ["prb_util", "rsrp"])
    assert assessment.supports_fine_tuning is True
    assert assessment.supports_full_retraining is True


def test_capability_no_fine_tuning_without_mechanism() -> None:
    assessment = assess_capability(_inspection(), ["prb_util", "rsrp"])
    assert assessment.supports_fine_tuning is False
    assert assessment.supports_full_retraining is True


def test_capability_blocks_everything_when_schema_incompatible() -> None:
    assessment = assess_capability(_inspection(supports_partial_fit=True), ["prb_util"])
    assert assessment.supports_fine_tuning is False
    assert assessment.supports_full_retraining is False
    assert assessment.schema_check.compatible is False


# ---- engine selection ---------------------------------------------------------------------------
def _capability(**overrides) -> CapabilityAssessment:
    defaults = {
        "supports_fine_tuning": True,
        "supports_full_retraining": True,
        "schema_check": SchemaCompatibility(compatible=True, reason="ok"),
        "reason": "ok",
    }
    defaults.update(overrides)
    return CapabilityAssessment(**defaults)


def test_select_engine_fine_tuning_sklearn() -> None:
    assert select_engine(Strategy.FINE_TUNING, "sklearn", _capability()) == (
        EngineKind.SKLEARN_PARTIAL_FIT
    )


def test_select_engine_fine_tuning_torch() -> None:
    assert select_engine(Strategy.FINE_TUNING, "torch", _capability()) == EngineKind.TORCH_FINE_TUNE


def test_select_engine_fine_tuning_raises_when_not_supported() -> None:
    with pytest.raises(UnsupportedAdaptationError):
        select_engine(Strategy.FINE_TUNING, "sklearn", _capability(supports_fine_tuning=False))


def test_select_engine_full_retraining_per_framework() -> None:
    assert (
        select_engine(Strategy.FULL_RETRAINING, "sklearn", _capability())
        == EngineKind.SKLEARN_FULL_RETRAIN
    )
    assert (
        select_engine(Strategy.FULL_RETRAINING, "xgboost", _capability())
        == EngineKind.XGBOOST_FULL_RETRAIN
    )
    assert (
        select_engine(Strategy.FULL_RETRAINING, "torch", _capability())
        == EngineKind.TORCH_FULL_RETRAIN
    )


def test_select_engine_full_retraining_raises_when_schema_incompatible() -> None:
    with pytest.raises(UnsupportedAdaptationError):
        select_engine(
            Strategy.FULL_RETRAINING, "sklearn", _capability(supports_full_retraining=False)
        )


def test_select_engine_raises_for_undefined_strategy() -> None:
    with pytest.raises(UnsupportedAdaptationError):
        select_engine(Strategy.ROLLBACK, "sklearn", _capability())
