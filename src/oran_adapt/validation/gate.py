"""The validation gate: may this candidate replace the incumbent?

Primary test - a paired bootstrap over the held-out rows. Both models are scored on the same
resampled rows, and the candidate's improvement on the task's primary metric (oriented so that
positive means better) gets a two-sided interval at GatePolicy.confidence. ``superiority``
accepts only when the interval lies wholly above the margin (so an interval that includes zero
is a rejection); ``non_inferiority`` accepts when it lies at or above minus the margin.

Guardrails - hard rejects whatever the primary metric says: per-slice regressions, calibration
(expected calibration error of binary classifiers), prediction latency, serialized size and
configured secondary metrics. Every threshold comes from the policy (core.policies.GatePolicy);
the decision records the policy's version and hash with every number it was based on.
"""

from __future__ import annotations

import io
import pickle
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass

import joblib
import numpy as np
import pandas as pd
from sklearn import metrics as skm

from oran_adapt.core.enums import TaskType
from oran_adapt.core.policies import GatePolicy, policy_hash
from oran_adapt.validation.metrics import anomaly_flags, higher_is_better
from oran_adapt.validation.schemas import GateDecision, GuardrailResult

TEST_NAME = "paired_bootstrap"

# Per-row values whose column means determine a metric, and the function from those means to
# the metric (vectorised over a batch of resamples: (batch, k) -> (batch,)).
Stat = Callable[[np.ndarray], np.ndarray]


@dataclass(frozen=True)
class Scored:
    """One model on the held-out rows: what it predicted, its ranking score (or None), its full
    metric set, and how to call it again (for the latency guardrail)."""

    model: object
    predictions: np.ndarray
    score: np.ndarray | None
    metrics: dict[str, float]
    run: Callable[[pd.DataFrame], object]


def _mean(m: np.ndarray) -> np.ndarray:
    return np.asarray(m[:, 0])


def _root_mean(m: np.ndarray) -> np.ndarray:
    return np.sqrt(m[:, 0])


def _f1(m: np.ndarray) -> np.ndarray:
    tp, fp, fn = m[:, 0], m[:, 1], m[:, 2]
    denominator = 2 * tp + fp + fn
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denominator > 0, 2 * tp / np.where(denominator > 0, denominator, 1), 0.0)


def _rows(metric: str, y: np.ndarray | None, pred: np.ndarray, X: np.ndarray | None,
          sample: np.ndarray) -> tuple[np.ndarray, Stat]:
    """Per-row values of ``metric`` (rows of ``sample`` for silhouette, else every row)."""
    if metric == "accuracy":
        assert y is not None
        return (y == pred).astype(float)[:, None], _mean
    if metric == "rmse":
        assert y is not None
        return ((y.astype(float) - pred.astype(float)) ** 2)[:, None], _root_mean
    if metric == "f1":
        assert y is not None
        truth, flagged = anomaly_flags(y), anomaly_flags(pred)
        cols = [truth & flagged, ~truth & flagged, truth & ~flagged]
        return np.stack(cols, axis=1).astype(float), _f1
    if metric == "silhouette":
        assert X is not None
        values = skm.silhouette_samples(X[sample], pred[sample])
        return np.asarray(values, dtype=float)[:, None], _mean
    raise ValueError(f"the gate has no per-row form of metric {metric!r}")


def _point(rows: np.ndarray, stat: Stat) -> float:
    return float(stat(rows.mean(axis=0)[None, :])[0])


def _bootstrap(policy: GatePolicy, cur: np.ndarray, cand: np.ndarray, stat: Stat,
               sign: float, rng: np.random.Generator) -> np.ndarray:
    n = len(cur)
    out: list[np.ndarray] = []
    for start in range(0, policy.resamples, policy.batch):
        size = min(policy.batch, policy.resamples - start)
        idx = rng.integers(0, n, size=(size, n))
        out.append(sign * (stat(cand[idx].mean(axis=1)) - stat(cur[idx].mean(axis=1))))
    return np.concatenate(out)


# ---- guardrails ------------------------------------------------------------------------------


def _slices(policy: GatePolicy, context: pd.DataFrame | None, rows_cur: np.ndarray,
            rows_cand: np.ndarray, stat: Stat, sign: float,
            per_row: bool) -> list[GuardrailResult]:
    guard = policy.slices
    out: list[GuardrailResult] = []
    for column in guard.columns:
        name = f"slice:{column}"
        if not per_row:
            out.append(GuardrailResult(name=name, passed=True, evaluated=False,
                                       detail="not applicable to this task's metric"))
            continue
        if context is None or column not in context.columns:
            out.append(GuardrailResult(
                name=name, passed=not guard.require_columns, evaluated=False,
                detail=f"column {column!r} is not in the held-out data",
            ))
            continue
        values = context[column].to_numpy()
        worst: dict[str, float] = {}
        failed: list[str] = []
        checked = 0
        for value in pd.unique(values):
            mask = values == value
            if int(mask.sum()) < guard.min_rows:
                continue
            checked += 1
            cur_v, cand_v = _point(rows_cur[mask], stat), _point(rows_cand[mask], stat)
            drop = sign * (cur_v - cand_v)
            allowed = guard.max_drop * (abs(cur_v) if guard.relative else 1.0)
            worst[str(value)] = round(drop, 6)
            if drop > allowed:
                failed.append(f"{value} (worse by {drop:.4f} > {allowed:.4f})")
        if checked == 0:
            out.append(GuardrailResult(name=name, passed=True, evaluated=False,
                                       detail=f"no slice has {guard.min_rows} rows"))
            continue
        out.append(GuardrailResult(
            name=name, passed=not failed, evaluated=True,
            detail="regressed slices: " + ", ".join(failed) if failed
            else f"{checked} slices within {guard.max_drop}",
            values={"drop_by_slice": worst},
        ))
    return out


def _ece(truth: np.ndarray, prob: np.ndarray, bins: int) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(prob, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        mask = which == b
        if mask.any():
            total += abs(float(truth[mask].mean()) - float(prob[mask].mean())) * mask.mean()
    return float(total)


def _positive(model: object) -> object | None:
    classes = list(getattr(model, "classes_", []))
    return classes[1] if len(classes) == 2 else None


def _calibration(policy: GatePolicy, task: TaskType, y: np.ndarray | None, current: Scored,
                 candidate: Scored) -> GuardrailResult | None:
    guard = policy.calibration
    if not guard.enabled:
        return None
    if task != TaskType.CLASSIFICATION or y is None or current.score is None \
            or candidate.score is None:
        return GuardrailResult(name="calibration", passed=True, evaluated=False,
                               detail="needs binary class probabilities from both models")
    ece = {}
    for label, scored in (("current", current), ("candidate", candidate)):
        positive = _positive(scored.model)
        score = scored.score
        assert score is not None  # both checked above
        ece[label] = _ece((y == positive).astype(float), score.astype(float), guard.bins)
    increase = ece["candidate"] - ece["current"]
    problems = []
    if increase > guard.max_increase:
        problems.append(f"ECE rose by {increase:.4f} > {guard.max_increase}")
    if guard.max_ece is not None and ece["candidate"] > guard.max_ece:
        problems.append(f"ECE {ece['candidate']:.4f} > {guard.max_ece}")
    return GuardrailResult(
        name="calibration", passed=not problems, evaluated=True,
        detail="; ".join(problems) or "calibration within policy",
        values={"ece_current": round(ece["current"], 6),
                "ece_candidate": round(ece["candidate"], 6)},
    )


def _median_ms(run: Callable[[pd.DataFrame], object], X: pd.DataFrame, repeats: int) -> float:
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        run(X)
        times.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(times)


def _latency(policy: GatePolicy, X: pd.DataFrame, current: Scored,
             candidate: Scored) -> GuardrailResult | None:
    guard = policy.latency
    if not guard.enabled:
        return None
    sample = X.iloc[: guard.max_rows]
    cur = _median_ms(current.run, sample, guard.repeats)
    cand = _median_ms(candidate.run, sample, guard.repeats)
    limit = guard.max_ratio * max(cur, guard.floor_ms)
    return GuardrailResult(
        name="latency", passed=cand <= limit, evaluated=True,
        detail=f"candidate {cand:.2f} ms vs limit {limit:.2f} ms",
        values={"current_ms": round(cur, 3), "candidate_ms": round(cand, 3),
                "limit_ms": round(limit, 3)},
    )


def serialized_bytes(model: object) -> int:
    buffer = io.BytesIO()
    joblib.dump(model, buffer)
    return buffer.getbuffer().nbytes


def _size(policy: GatePolicy, current: Scored, candidate: Scored) -> GuardrailResult | None:
    guard = policy.size
    if not guard.enabled:
        return None
    try:
        cur, cand = serialized_bytes(current.model), serialized_bytes(candidate.model)
    except (pickle.PicklingError, TypeError, AttributeError) as exc:
        return GuardrailResult(name="size", passed=True, evaluated=False,
                               detail=f"model is not serializable with joblib: {exc}")
    limit = guard.max_ratio * max(cur, guard.floor_bytes)
    if guard.max_bytes is not None:
        limit = min(limit, guard.max_bytes)
    return GuardrailResult(
        name="size", passed=cand <= limit, evaluated=True,
        detail=f"candidate {cand} bytes vs limit {int(limit)} bytes",
        values={"current_bytes": cur, "candidate_bytes": cand, "limit_bytes": int(limit)},
    )


def _metric_guards(policy: GatePolicy, current: Scored,
                   candidate: Scored) -> list[GuardrailResult]:
    out = []
    for metric, max_drop in sorted(policy.metric_guards.items()):
        name = f"metric:{metric}"
        try:
            sign = 1.0 if higher_is_better(metric) else -1.0
        except ValueError:
            out.append(GuardrailResult(name=name, passed=False, evaluated=False,
                                       detail=f"unknown metric {metric!r}"))
            continue
        if metric not in current.metrics or metric not in candidate.metrics:
            out.append(GuardrailResult(name=name, passed=True, evaluated=False,
                                       detail="metric not defined for both models"))
            continue
        drop = sign * (current.metrics[metric] - candidate.metrics[metric])
        out.append(GuardrailResult(
            name=name, passed=drop <= max_drop, evaluated=True,
            detail=f"worse by {drop:.4f} (allowed {max_drop})",
            values={"current": current.metrics[metric], "candidate": candidate.metrics[metric]},
        ))
    return out


# ---- decision --------------------------------------------------------------------------------


def decide(
    policy: GatePolicy,
    *,
    task: TaskType,
    metric: str,
    X: pd.DataFrame,
    y: pd.Series | None,
    current: Scored,
    candidate: Scored,
    context: pd.DataFrame | None = None,
) -> GateDecision:
    """The gate's decision for ``candidate`` against ``current`` on the held-out rows.
    ``context`` is the held-out frame with any non-feature columns the slice guard names,
    row-aligned with ``X``."""
    better_high = higher_is_better(metric)
    sign = 1.0 if better_high else -1.0
    rng = np.random.default_rng(policy.seed)
    n = len(X)
    y_arr = None if y is None else np.asarray(y)
    X_arr = X.to_numpy(dtype=float) if metric == "silhouette" else None
    limit = policy.pairwise_max_rows if metric == "silhouette" else n
    pair_sample = np.sort(rng.choice(n, size=limit, replace=False)) if n > limit else np.arange(n)

    rows_cur, stat = _rows(metric, y_arr, current.predictions, X_arr, pair_sample)
    rows_cand, _ = _rows(metric, y_arr, candidate.predictions, X_arr, pair_sample)
    boot_cur, boot_cand = rows_cur, rows_cand
    if len(rows_cur) > policy.max_rows:
        pick = rng.choice(len(rows_cur), size=policy.max_rows, replace=False)
        boot_cur, boot_cand = rows_cur[pick], rows_cand[pick]
    diffs = _bootstrap(policy, boot_cur, boot_cand, stat, sign, rng)
    alpha = 1.0 - policy.confidence
    ci_low = float(np.quantile(diffs, alpha / 2))
    ci_high = float(np.quantile(diffs, 1 - alpha / 2))

    current_value = float(current.metrics.get(metric, _point(rows_cur, stat)))
    candidate_value = float(candidate.metrics.get(metric, _point(rows_cand, stat)))
    delta = sign * (candidate_value - current_value)
    base = policy.margin * (abs(current_value) if policy.margin_relative else 1.0)
    if policy.mode == "superiority":
        threshold = base
        primary_ok = ci_low > threshold
        wanted = f"improvement interval must lie above {threshold:.4f}"
    else:
        threshold = -base
        primary_ok = ci_low >= threshold
        wanted = f"improvement interval must not go below {threshold:.4f}"

    guardrails = _slices(policy, context, rows_cur, rows_cand, stat, sign,
                         per_row=metric != "silhouette")
    for extra in (_calibration(policy, task, y_arr, current, candidate),
                  _latency(policy, X, current, candidate),
                  _size(policy, current, candidate)):
        if extra is not None:
            guardrails.append(extra)
    guardrails.extend(_metric_guards(policy, current, candidate))

    reasons = []
    if not primary_ok:
        reasons.append(
            f"{metric}: candidate {candidate_value:.4f} vs current {current_value:.4f}, "
            f"improvement {delta:+.4f} with {policy.confidence:.0%} interval "
            f"[{ci_low:+.4f}, {ci_high:+.4f}]; {policy.mode} {wanted}"
        )
    reasons.extend(f"guardrail {g.name} failed: {g.detail}" for g in guardrails if not g.passed)
    verdict = "REJECT" if reasons else "ACCEPT"
    if not reasons:
        reasons.append(
            f"{metric}: candidate {candidate_value:.4f} vs current {current_value:.4f}, "
            f"improvement {delta:+.4f} with {policy.confidence:.0%} interval "
            f"[{ci_low:+.4f}, {ci_high:+.4f}] passes {policy.mode}; all guardrails passed"
        )
    return GateDecision(
        verdict=verdict,
        reasons=reasons,
        policy_version=policy.version,
        policy_hash=policy_hash(policy),
        mode=policy.mode,
        test=TEST_NAME,
        metric=metric,
        higher_is_better=better_high,
        current_value=current_value,
        candidate_value=candidate_value,
        delta=delta,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence=policy.confidence,
        threshold=threshold,
        resamples=policy.resamples,
        n_rows=n,
        guardrails=guardrails,
    )
