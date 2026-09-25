"""Member 1 - comparison: quantify how much the drifted segment differs from the baseline.

Two independent, real statistics per numeric feature, both computed by Evidently AI's
ValueDrift metric: the two-sample Kolmogorov-Smirnov test (is the drifted distribution shape
different from the historical one?) and the Population Stability Index (how much has the
distribution moved, in the units PSI practitioners use to threshold on: <0.1 no significant
shift, 0.1-0.25 moderate, >0.25 major). Evidently reports only the KS p-value, so the KS
statistic itself comes from scipy. Only features present as numeric values in *both* segments
are compared; everything else is left for a future phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from evidently import DataDefinition, Dataset, Report
from evidently.metrics import ValueDrift
from scipy import stats

from oran_adapt.analysis.merge import MergedSeries

_RESERVED_KEYS = {"observed_at", "segment"}


@dataclass
class FeatureComparison:
    feature: str
    historical_mean: float
    drifted_mean: float
    historical_std: float
    drifted_std: float
    ks_statistic: float
    ks_pvalue: float
    psi: float
    # Rows behind this feature's statistics in each segment (a row may lack a feature).
    n_historical: int = 0
    n_drifted: int = 0


@dataclass
class ComparisonResult:
    features: list[FeatureComparison] = field(default_factory=list)
    historical_count: int = 0
    drifted_count: int = 0
    max_psi: float = 0.0
    min_ks_pvalue: float = 1.0


def _numeric_feature_names(rows: list[dict]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        for k, v in row.items():
            if k in _RESERVED_KEYS:
                continue
            if isinstance(v, int | float) and not isinstance(v, bool):
                names.add(k)
    return names


def _evidently_drift(
    historical: dict[str, np.ndarray], drifted: dict[str, np.ndarray]
) -> dict[tuple[str, str], float]:
    """Run one Evidently report (historical = reference, drifted = current) and return
    {(feature, "ks" | "psi"): value}; for "ks" the value is the p-value."""
    columns = sorted(historical)
    definition = DataDefinition(numerical_columns=columns)

    def dataset(values: dict[str, np.ndarray]) -> Dataset:
        # Features can have different row counts (a key missing from some rows), so pad to
        # equal length; Evidently drops the NaNs per column.
        frame = pd.DataFrame({c: pd.Series(values[c]) for c in columns})
        return Dataset.from_pandas(frame, data_definition=definition)

    metrics = [ValueDrift(column=c, method=m) for c in columns for m in ("ks", "psi")]
    snapshot = Report(metrics).run(dataset(drifted), dataset(historical))
    return {
        (m["config"]["column"], m["config"]["method"]): float(m["value"])
        for m in snapshot.dict()["metrics"]
    }


def compare_segments(merged: MergedSeries) -> ComparisonResult:
    historical_rows = [r for r in merged.rows if r["segment"] == "historical"]
    drifted_rows = [r for r in merged.rows if r["segment"] == "drifted"]
    result = ComparisonResult(
        historical_count=len(historical_rows), drifted_count=len(drifted_rows)
    )
    if not historical_rows or not drifted_rows:
        return result

    features = _numeric_feature_names(historical_rows) & _numeric_feature_names(drifted_rows)
    historical: dict[str, np.ndarray] = {}
    drifted: dict[str, np.ndarray] = {}
    for feature in sorted(features):
        h = np.array([r[feature] for r in historical_rows if feature in r], dtype=float)
        d = np.array([r[feature] for r in drifted_rows if feature in r], dtype=float)
        if h.size >= 2 and d.size >= 2:
            historical[feature], drifted[feature] = h, d
    if not historical:
        return result

    drift = _evidently_drift(historical, drifted)
    for feature, h in historical.items():
        d = drifted[feature]
        result.features.append(
            FeatureComparison(
                feature=feature,
                historical_mean=float(h.mean()),
                drifted_mean=float(d.mean()),
                historical_std=float(h.std()),
                drifted_std=float(d.std()),
                ks_statistic=float(stats.ks_2samp(h, d).statistic),
                ks_pvalue=drift[(feature, "ks")],
                psi=drift[(feature, "psi")],
                n_historical=int(h.size),
                n_drifted=int(d.size),
            )
        )

    if result.features:
        result.max_psi = max(f.psi for f in result.features)
        result.min_ks_pvalue = min(f.ks_pvalue for f in result.features)
    return result
