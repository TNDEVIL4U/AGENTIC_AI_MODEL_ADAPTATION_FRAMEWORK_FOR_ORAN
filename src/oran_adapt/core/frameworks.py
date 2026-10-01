"""The built-in adaptation engines and the frameworks they handle: which engine carries out
each strategy for scikit-learn, XGBoost and tabular torch models.

Only the built-in model type plugins (oran_adapt.adapters.model_types.tabular) read this table.
Which frameworks the service adapts is whatever the installed model type plugins declare
(oran_adapt.adaptation.model_types): a new framework is a new plugin, not a row here. Importing
it pulls in no ML library.
"""

from __future__ import annotations

from collections.abc import Mapping

from oran_adapt.core.enums import EngineKind, Strategy

# Frameworks whose artifacts are torch.nn.Module objects, under either name MLflow records.
TORCH_FRAMEWORKS = frozenset({"torch", "pytorch"})
# Frameworks whose artifacts follow the scikit-learn estimator API.
SKLEARN_API_FRAMEWORKS = frozenset({"sklearn", "xgboost"})

ENGINES: Mapping[Strategy, Mapping[str, EngineKind]] = {
    Strategy.FINE_TUNING: {
        "sklearn": EngineKind.SKLEARN_PARTIAL_FIT,
        **dict.fromkeys(TORCH_FRAMEWORKS, EngineKind.TORCH_FINE_TUNE),
    },
    Strategy.FULL_RETRAINING: {
        "sklearn": EngineKind.SKLEARN_FULL_RETRAIN,
        "xgboost": EngineKind.XGBOOST_FULL_RETRAIN,
        **dict.fromkeys(TORCH_FRAMEWORKS, EngineKind.TORCH_FULL_RETRAIN),
    },
}

def engine_for(strategy: Strategy, framework: str) -> EngineKind | None:
    return ENGINES.get(strategy, {}).get(framework.lower())
