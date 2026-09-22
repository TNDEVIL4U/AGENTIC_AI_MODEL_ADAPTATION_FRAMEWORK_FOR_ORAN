"""Member 1 - comparison: quantify how much the drifted segment differs from the baseline.

Two independent, real statistics per numeric feature: the two-sample Kolmogorov-Smirnov test
(is the drifted distribution shape different from the historical one?) and the Population
Stability Index (how much has the distribution moved, in the units PSI practitioners use to
threshold on: <0.1 no significant shift, 0.1-0.25 moderate, >0.25 major). Only features present
as numeric values in *both* segments are compared; everything else is left for a future phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import stats

from oran_adapt.analysis.merge import MergedSeries

_RESERVED_KEYS = {"observed_at", "segment"}
_PSI_BINS = 10


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


def _population_stability_index(
    baseline: np.ndarray, current: np.ndarray, bins: int = _PSI_BINS
) -> float:
    edges = np.unique(np.quantile(baseline, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    b_counts, _ = np.histogram(baseline, bins=edges)
    c_counts, _ = np.histogram(current, bins=edges)
    b_frac = np.clip(b_counts / max(len(baseline), 1), 1e-6, None)
    c_frac = np.clip(c_counts / max(len(current), 1), 1e-6, None)
    return float(np.sum((c_frac - b_frac) * np.log(c_frac / b_frac)))


def compare_segments(merged: MergedSeries) -> ComparisonResult:
    historical_rows = [r for r in merged.rows if r["segment"] == "historical"]
    drifted_rows = [r for r in merged.rows if r["segment"] == "drifted"]
    result = ComparisonResult(
        historical_count=len(historical_rows), drifted_count=len(drifted_rows)
    )
    if not historical_rows or not drifted_rows:
        return result

    features = _numeric_feature_names(historical_rows) & _numeric_feature_names(drifted_rows)
    for feature in sorted(features):
        h = np.array([r[feature] for r in historical_rows if feature in r], dtype=float)
        d = np.array([r[feature] for r in drifted_rows if feature in r], dtype=float)
        if h.size < 2 or d.size < 2:
            continue
        ks_stat, ks_p = stats.ks_2samp(h, d)
        psi = _population_stability_index(h, d)
        result.features.append(
            FeatureComparison(
                feature=feature,
                historical_mean=float(h.mean()),
                drifted_mean=float(d.mean()),
                historical_std=float(h.std()),
                drifted_std=float(d.std()),
                ks_statistic=float(ks_stat),
                ks_pvalue=float(ks_p),
                psi=psi,
            )
        )

    if result.features:
        result.max_psi = max(f.psi for f in result.features)
        result.min_ks_pvalue = min(f.ks_pvalue for f in result.features)
    return result
