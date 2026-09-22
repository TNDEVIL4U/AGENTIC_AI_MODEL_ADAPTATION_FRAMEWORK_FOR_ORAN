"""Member 3 - inspector: look at a real, loaded model object and report what it actually is and
can do. Never trust ModelMetadata.framework/model_type alone - this is the ground truth check
against the artifact itself.
"""

from __future__ import annotations

from oran_adapt.adaptation.schemas import ModelInspection
from oran_adapt.core.errors import UnsupportedAdaptationError

_SKLEARN_LIKE = {"sklearn", "xgboost"}
_TORCH_LIKE = {"torch", "pytorch"}


def _inspect_sklearn_like(model: object, framework: str) -> ModelInspection:
    from sklearn.base import is_classifier, is_regressor

    estimator_type = "unknown"
    if is_classifier(model):
        estimator_type = "classifier"
    elif is_regressor(model):
        estimator_type = "regressor"

    feature_names = getattr(model, "feature_names_in_", None)
    params = model.get_params() if hasattr(model, "get_params") else {}

    return ModelInspection(
        framework=framework,
        model_class=type(model).__name__,
        estimator_type=estimator_type,
        n_features_in=getattr(model, "n_features_in_", None),
        feature_names_in=list(feature_names) if feature_names is not None else None,
        supports_partial_fit=hasattr(model, "partial_fit"),
        supports_warm_start=bool(params.get("warm_start", False)),
    )


def _inspect_torch(model: object) -> ModelInspection:
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
    fw = framework.lower()
    if fw in _SKLEARN_LIKE:
        return _inspect_sklearn_like(model, fw)
    if fw in _TORCH_LIKE:
        return _inspect_torch(model)
    raise UnsupportedAdaptationError(f"no inspector for framework {framework!r}")
