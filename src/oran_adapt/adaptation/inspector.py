"""Member 3 - inspector: look at a real, loaded model object and report what it actually is and
can do. Never trust ModelMetadata.framework/model_type alone - this is the ground truth check
against the artifact itself.

Which kind of model it is decides who looks: the model type plugin that recognises it
(oran_adapt.adaptation.model_types). The helpers here describe the two families the built-in
plugins share - scikit-learn-API estimators and tabular torch modules.
"""

from __future__ import annotations

from oran_adapt.adaptation.schemas import ModelInspection
from oran_adapt.core.errors import UnsupportedAdaptationError


def _sklearn_estimator_type(model: object) -> str:
    from sklearn.base import is_classifier, is_clusterer, is_outlier_detector, is_regressor

    if not hasattr(model, "__sklearn_tags__"):
        # A duck-typed estimator (the scikit-learn API without BaseEstimator) has no tags for
        # sklearn to read; the declared ``_estimator_type`` is all there is.
        declared = getattr(model, "_estimator_type", None)
        return declared if isinstance(declared, str) else "unknown"
    if is_classifier(model):
        return "classifier"
    if is_regressor(model):
        return "regressor"
    if is_clusterer(model):
        return "clusterer"
    if is_outlier_detector(model):
        return "outlier_detector"
    return "unknown"


def inspect_sklearn_like(model: object, framework: str) -> ModelInspection:
    estimator_type = _sklearn_estimator_type(model)

    feature_names = getattr(model, "feature_names_in_", None)
    steps = getattr(model, "steps", None)  # sklearn Pipeline: [(name, transformer), ..., final]
    preprocessing = [type(t).__name__ for _, t in steps[:-1]] if steps else []
    final = steps[-1][1] if steps else model
    params = final.get_params() if hasattr(final, "get_params") else {}
    classes = getattr(model, "classes_", None)

    return ModelInspection(
        framework=framework,
        model_class=type(model).__name__,
        estimator_type=estimator_type,
        n_features_in=getattr(model, "n_features_in_", None),
        feature_names_in=list(feature_names) if feature_names is not None else None,
        supports_partial_fit=hasattr(model, "partial_fit"),
        supports_warm_start=bool(params.get("warm_start", False)),
        preprocessing_steps=preprocessing,
        classes=[str(c) for c in classes] if classes is not None else None,
    )


def inspect_torch(model: object) -> ModelInspection:
    from torch import nn

    if not isinstance(model, nn.Module):
        raise UnsupportedAdaptationError(
            f"expected a torch.nn.Module, got {type(model).__name__}"
        )

    n_parameters = sum(p.numel() for p in model.parameters())
    linears = [m for m in model.modules() if isinstance(m, nn.Linear)]
    input_dim = linears[0].in_features if linears else None
    output_dim = linears[-1].out_features if linears else None

    # No task label is attached to a bare nn.Module, so infer it from the final layer's width:
    # >1 output units means per-class logits (classifier), exactly 1 means a scalar (regressor).
    if output_dim is None:
        estimator_type = "unknown"
    elif output_dim > 1:
        estimator_type = "classifier"
    else:
        estimator_type = "regressor"

    return ModelInspection(
        framework="torch",
        model_class=type(model).__name__,
        estimator_type=estimator_type,
        supports_partial_fit=False,
        # A torch module can always resume training from its current weights.
        supports_warm_start=True,
        n_parameters=n_parameters,
        input_dim=input_dim,
        output_dim=output_dim,
    )


def inspect_model(model: object, framework: str) -> ModelInspection:
    """What the process's model type plugins say ``model`` is. Raises
    UnsupportedModelTypeError (an UnsupportedAdaptationError) when none recognises it."""
    from oran_adapt.adaptation.model_types import default_model_types

    return default_model_types().require(model, framework)
