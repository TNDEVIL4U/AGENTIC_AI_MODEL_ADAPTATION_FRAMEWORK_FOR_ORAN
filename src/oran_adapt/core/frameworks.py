"""The model frameworks the adaptation engines handle, and which engine carries out each
strategy for each of them.

This table is the one place a framework name maps to an engine. Engine selection, the drift
summary's capability flags, the inspector, validation scoring, the CLI and the default of
decision_supported_frameworks all read it, so adding
a framework means adding its rows here and its engine, nothing else. Importing it pulls in no ML
library.
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

ADAPTABLE_FRAMEWORKS = frozenset(fw for table in ENGINES.values() for fw in table)


def frameworks_for(strategy: Strategy) -> frozenset[str]:
    """Frameworks with a built-in engine for ``strategy`` (empty for a strategy without one)."""
    return frozenset(ENGINES.get(strategy, {}))


def engine_for(strategy: Strategy, framework: str) -> EngineKind | None:
    return ENGINES.get(strategy, {}).get(framework.lower())
