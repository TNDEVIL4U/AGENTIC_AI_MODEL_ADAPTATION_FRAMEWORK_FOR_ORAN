"""Kill tests for scripts/mutation.py: plain, fixture-free functions named ``kill_*`` that pin
the job state machine and the validation gate exactly, so a mutant of either is caught.
``test_phase13_mutation.py`` runs them against the real code as ordinary tests too.

The state machine's allowed moves are written out here independently of ``_ALLOWED``. The gate
is pinned two ways: behavioural checks at every threshold boundary, and golden decisions
(``mutation_golden.json``, regenerated with ``scripts/mutation.py --write-golden`` after an
intended change) for a set of seeded scenarios covering every metric and guardrail.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from oran_adapt.core.enums import JobStatus, TaskType
from oran_adapt.core.errors import InvalidTransitionError
from oran_adapt.core.policies import GatePolicy
from oran_adapt.core.state_machine import allowed_next, check_transition
from oran_adapt.validation.gate import Scored, decide, serialized_bytes
from oran_adapt.validation.schemas import GateDecision

GOLDEN = Path(__file__).with_name("mutation_golden.json")
S = JobStatus

# ---- state machine -------------------------------------------------------------------------------
_ANY_TIME = {S.TIMED_OUT, S.CANCELLED}
_RETRY = {S.QUEUED}
EXPECTED: dict[JobStatus, frozenset[JobStatus]] = {
    S.RECEIVED: frozenset({S.QUEUED, S.VALIDATING, S.FAILED} | _ANY_TIME),
    S.QUEUED: frozenset({S.VALIDATING, S.FAILED} | _ANY_TIME),
    S.VALIDATING: frozenset({S.DATA_PREPARING, S.FAILED} | _ANY_TIME | _RETRY),
    S.DATA_PREPARING: frozenset({S.EVALUATING_VERSIONS, S.DECISION_PENDING, S.COMPLETED,
                                 S.FAILED} | _ANY_TIME | _RETRY),
    S.EVALUATING_VERSIONS: frozenset({S.REUSE_DECISION, S.FAILED} | _ANY_TIME | _RETRY),
    S.REUSE_DECISION: frozenset({S.PROMOTING, S.DECISION_PENDING, S.FAILED}
                                | _ANY_TIME | _RETRY),
    S.DECISION_PENDING: frozenset({S.ADAPTING, S.COMPLETED, S.FAILED} | _ANY_TIME | _RETRY),
    S.ADAPTING: frozenset({S.VALIDATING_CANDIDATE, S.FAILED} | _ANY_TIME | _RETRY),
    S.VALIDATING_CANDIDATE: frozenset({S.REGISTERING, S.COMPLETED, S.FAILED}
                                      | _ANY_TIME | _RETRY),
    S.REGISTERING: frozenset({S.PROMOTING, S.FAILED} | _ANY_TIME | _RETRY),
    S.PROMOTING: frozenset({S.COMPLETED, S.ROLLED_BACK, S.FAILED} | _ANY_TIME | _RETRY),
    # Terminal: nothing follows.
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.ROLLED_BACK: frozenset(),
    S.TIMED_OUT: frozenset(),
    S.CANCELLED: frozenset(),
    # Legacy statuses (rows written before Phase 14): only failure, the deadline, a cancel or
    # a retry can follow.
    S.ANALYZING: frozenset({S.FAILED} | _ANY_TIME | _RETRY),
    S.MODEL_COMPARISON: frozenset({S.FAILED} | _ANY_TIME | _RETRY),
    S.DECISION_MADE: frozenset({S.FAILED} | _ANY_TIME | _RETRY),
}


def kill_state_machine_table_is_exact() -> None:
    assert set(EXPECTED) == set(JobStatus), "every status needs a row in EXPECTED"
    for status, expected in EXPECTED.items():
        got = allowed_next(status)
        assert isinstance(got, frozenset), status
        assert got == expected, (status, sorted(got ^ expected))


def kill_check_transition_agrees_with_the_table() -> None:
    for current, allowed in EXPECTED.items():
        for target in JobStatus:
            if target in allowed:
                check_transition(current, target)
                check_transition(current.value, target.value)
                continue
            try:
                check_transition(current, target)
            except InvalidTransitionError as exc:
                assert exc.context["from_status"] == current, exc.context
                assert exc.context["to_status"] == target, exc.context
            else:
                raise AssertionError(f"{current} -> {target} must be refused")


# ---- gate ----------------------------------------------------------------------------------------
N = 80


def _policy(**overrides: object) -> GatePolicy:
    base: dict[str, object] = {
        "resamples": 200, "batch": 50, "seed": 7,
        "latency": {"enabled": False}, "size": {"enabled": False},
        "calibration": {"enabled": False},
    }
    base.update(overrides)
    return GatePolicy.model_validate(base)


def _model(**extra: object) -> SimpleNamespace:
    return SimpleNamespace(classes_=np.array([0, 1]), **extra)


def _scored(pred: np.ndarray, *, score: np.ndarray | None = None,
            metrics: dict[str, float] | None = None, model: object | None = None) -> Scored:
    return Scored(model=model if model is not None else _model(), predictions=pred, score=score,
                  metrics=metrics or {}, run=lambda X: None)


def _labels() -> np.ndarray:
    return np.random.default_rng(1).integers(0, 2, N)


def _flip(y: np.ndarray, rows: range | list[int]) -> np.ndarray:
    out = y.copy()
    idx = list(rows)
    out[idx] = 1 - out[idx]
    return out


def _X(n: int = N) -> pd.DataFrame:
    rng = np.random.default_rng(2)
    return pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})


def _accuracy(policy: GatePolicy, current: np.ndarray, candidate: np.ndarray,
              **kw: object) -> GateDecision:
    y = _labels()
    return decide(policy, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
                  y=pd.Series(y), current=_scored(current), candidate=_scored(candidate),
                  **kw)  # type: ignore[arg-type]


def _guard(decision: GateDecision, name: str):
    matches = [g for g in decision.guardrails if g.name == name]
    assert len(matches) == 1, (name, [g.name for g in decision.guardrails])
    return matches[0]


def kill_superiority_needs_the_interval_above_the_margin() -> None:
    y = _labels()
    better = _accuracy(_policy(), _flip(y, range(16)), _flip(y, range(4)))
    assert better.verdict == "ACCEPT" and better.ci_low > 0, better
    assert math.isclose(better.current_value, 0.8) and math.isclose(better.candidate_value, 0.95)
    assert math.isclose(better.delta, 0.15) and better.higher_is_better
    same = _accuracy(_policy(), _flip(y, range(16)), _flip(y, range(16)))
    # Identical predictions: every resample's difference is 0, so ci_low == threshold == 0.
    assert (same.ci_low, same.ci_high, same.threshold) == (0.0, 0.0, 0.0)
    assert same.verdict == "REJECT", "superiority must reject an interval that touches 0"


def kill_non_inferiority_accepts_at_minus_the_margin() -> None:
    y = _labels()
    same = _accuracy(_policy(mode="non_inferiority"), _flip(y, range(16)), _flip(y, range(16)))
    assert same.verdict == "ACCEPT" and same.threshold == 0.0, same
    worse = _accuracy(_policy(mode="non_inferiority", margin=0.05), _flip(y, range(16)),
                      _flip(y, range(28)))
    assert worse.verdict == "REJECT" and worse.threshold == -0.05, worse
    relative = _accuracy(_policy(mode="non_inferiority", margin=0.5, margin_relative=True),
                         _flip(y, range(16)), _flip(y, range(28)))
    assert math.isclose(relative.threshold, -0.4) and relative.verdict == "ACCEPT", relative


def kill_error_metrics_are_oriented() -> None:
    rng = np.random.default_rng(3)
    y = rng.normal(size=N)
    current, candidate = y + rng.normal(0, 1.0, N), y + rng.normal(0, 0.1, N)
    d = decide(_policy(), task=TaskType.REGRESSION, metric="rmse", X=_X(), y=pd.Series(y),
               current=_scored(current), candidate=_scored(candidate))
    assert not d.higher_is_better and d.verdict == "ACCEPT" and d.delta > 0 and d.ci_low > 0, d
    assert math.isclose(d.current_value, float(np.sqrt(np.mean((y - current) ** 2))))
    back = decide(_policy(), task=TaskType.REGRESSION, metric="rmse", X=_X(), y=pd.Series(y),
                  current=_scored(candidate), candidate=_scored(current))
    assert back.verdict == "REJECT" and back.delta < 0 and back.ci_high < 0, back


def kill_f1_counts_anomalies() -> None:
    truth = np.zeros(N, dtype=int)
    truth[:10] = 1
    current = truth.copy()
    current[:5] = 0  # 5 of 10 anomalies found: f1 = 2*5 / (2*5 + 0 + 5)
    candidate = truth.copy()
    candidate[10:12] = 1  # all 10 found, 2 false alarms: f1 = 20 / 22
    d = decide(_policy(), task=TaskType.ANOMALY_DETECTION, metric="f1", X=_X(),
               y=pd.Series(truth), current=_scored(current), candidate=_scored(candidate))
    assert math.isclose(d.current_value, 10 / 15) and math.isclose(d.candidate_value, 20 / 22)
    silent = decide(_policy(mode="non_inferiority"), task=TaskType.ANOMALY_DETECTION,
                    metric="f1", X=_X(), y=pd.Series(np.zeros(N, dtype=int)),
                    current=_scored(np.zeros(N, dtype=int)),
                    candidate=_scored(np.zeros(N, dtype=int)))
    assert silent.current_value == 0.0 and silent.verdict == "ACCEPT", silent


def kill_slice_guard_boundaries() -> None:
    y = _labels()
    cells = np.array(["A"] * 32 + ["B"] * 32 + ["C"] * 16)
    context = pd.DataFrame({"cell": cells})
    current = y.copy()
    exact = _flip(y, range(8))  # cell A: 1.0 -> 0.75, a drop of exactly 0.25
    policy = _policy(mode="non_inferiority", margin=1.0,
                     slices={"columns": ["cell"], "max_drop": 0.25, "min_rows": 16})
    ok = _guard(_accuracy(policy, current, exact, context=context), "slice:cell")
    assert ok.passed and ok.evaluated and ok.values["drop_by_slice"] == {
        "A": 0.25, "B": 0.0, "C": 0.0}, ok
    over = _guard(_accuracy(policy, current, _flip(y, range(9)), context=context), "slice:cell")
    assert not over.passed and "A (worse by 0.2812" in over.detail, over
    small = _policy(mode="non_inferiority", margin=1.0,
                    slices={"columns": ["cell"], "max_drop": 0.25, "min_rows": 17})
    skipped = _guard(_accuracy(small, current, exact, context=context), "slice:cell")
    assert set(skipped.values["drop_by_slice"]) == {"A", "B"}, skipped
    rel = _policy(mode="non_inferiority", margin=1.0,
                  slices={"columns": ["cell"], "max_drop": 0.2, "relative": True, "min_rows": 16})
    assert not _guard(_accuracy(rel, current, exact, context=context), "slice:cell").passed
    none = _policy(slices={"columns": ["cell"], "min_rows": 64})
    empty = _guard(_accuracy(none, current, exact, context=context), "slice:cell")
    assert empty.passed and not empty.evaluated, empty


def kill_slice_guard_missing_column() -> None:
    y = _labels()
    lax = _guard(_accuracy(_policy(slices={"columns": ["cell"]}), y, y), "slice:cell")
    assert lax.passed and not lax.evaluated, lax
    strict = _accuracy(_policy(mode="non_inferiority",
                               slices={"columns": ["cell"], "require_columns": True}), y, y,
                       context=pd.DataFrame({"other": np.zeros(N)}))
    assert not _guard(strict, "slice:cell").passed and strict.verdict == "REJECT", strict


def _calibrated(current_p: float, candidate_p: float, **guard: object) -> GateDecision:
    y = np.array([0, 1] * (N // 2))
    policy = _policy(mode="non_inferiority", margin=1.0, calibration={"enabled": True, **guard})
    return decide(policy, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
                  y=pd.Series(y), current=_scored(y, score=np.full(N, current_p)),
                  candidate=_scored(y, score=np.full(N, candidate_p)))


def kill_calibration_boundaries() -> None:
    # Half the rows are positive: ECE of a constant p is |0.5 - p| exactly.
    exact = _guard(_calibrated(0.5, 0.75, max_increase=0.25), "calibration")
    assert exact.passed and exact.values == {"ece_current": 0.0, "ece_candidate": 0.25}, exact
    over = _guard(_calibrated(0.5, 0.875, max_increase=0.25), "calibration")
    assert not over.passed and "ECE rose" in over.detail, over
    cap = _guard(_calibrated(0.5, 0.75, max_increase=1.0, max_ece=0.25), "calibration")
    assert cap.passed, cap
    capped = _guard(_calibrated(0.5, 0.875, max_increase=1.0, max_ece=0.25), "calibration")
    assert not capped.passed and "ECE 0.3750 > 0.25" in capped.detail, capped
    better = _guard(_calibrated(0.875, 0.5, max_increase=0.0), "calibration")
    assert better.passed and better.values["ece_current"] == 0.375, better
    y = np.array([0, 1] * (N // 2))
    regression = decide(_policy(calibration={"enabled": True}), task=TaskType.REGRESSION,
                        metric="rmse", X=_X(), y=pd.Series(y.astype(float)),
                        current=_scored(y.astype(float)), candidate=_scored(y.astype(float)))
    skipped = _guard(regression, "calibration")
    assert skipped.passed and not skipped.evaluated, skipped
    assert all(g.name != "calibration" for g in _accuracy(_policy(), y, y).guardrails)


def kill_calibration_bins_are_exact() -> None:
    """Five bins of 16 rows at p = 0.1 .. 0.9 whose gaps alternate in sign, so merging or
    shifting any two bins changes the ECE: (0.15 + 0.3 + 0.25 + 0.2 + 0.1) / 5 = 0.2."""
    p = np.repeat([0.1, 0.3, 0.5, 0.7, 0.9], 16)
    y = np.concatenate([np.r_[np.ones(k), np.zeros(16 - k)] for k in (4, 0, 12, 8, 16)])
    y = y.astype(int)
    policy = _policy(mode="non_inferiority", margin=1.0,
                     calibration={"enabled": True, "bins": 5, "max_increase": 0.0})
    d = decide(policy, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
               y=pd.Series(y), current=_scored(y, score=p),
               candidate=_scored(y, score=np.full(N, 1 / 3)))
    guard = _guard(d, "calibration")
    # The candidate: half the rows positive, all at 1/3, so its ECE is 1/6.
    assert guard.values == {"ece_current": 0.2, "ece_candidate": 0.166667}, guard
    assert guard.passed and guard.evaluated, guard


def kill_size_boundaries() -> None:
    y = _labels()
    small, big = _model(blob=b"x" * 100), _model(blob=b"x" * 5000)
    cand_bytes, cur_bytes = serialized_bytes(big), serialized_bytes(small)

    def sized(**guard: object) -> object:
        policy = _policy(mode="non_inferiority", margin=1.0, size={"enabled": True, **guard})
        d = decide(policy, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
                   y=pd.Series(y), current=_scored(y, model=small),
                   candidate=_scored(y, model=big))
        return _guard(d, "size")

    exact = sized(max_ratio=1.0, floor_bytes=cand_bytes)
    assert exact.passed and exact.evaluated and exact.values["limit_bytes"] == cand_bytes, exact
    assert exact.values["current_bytes"] == cur_bytes
    assert not sized(max_ratio=1.0, floor_bytes=cand_bytes - 1).passed
    ratio = sized(max_ratio=cand_bytes / cur_bytes, floor_bytes=0)
    assert ratio.values["limit_bytes"] == cand_bytes, ratio
    capped = sized(max_ratio=100.0, floor_bytes=0, max_bytes=cand_bytes - 1)
    assert not capped.passed and capped.values["limit_bytes"] == cand_bytes - 1, capped
    unpicklable = decide(_policy(size={"enabled": True}), task=TaskType.CLASSIFICATION,
                         metric="accuracy", X=_X(), y=pd.Series(y), current=_scored(y),
                         candidate=_scored(y, model=_model(fn=lambda: 0)))
    unsized = _guard(unpicklable, "size")
    assert unsized.passed and not unsized.evaluated, unsized


def kill_metric_guard_boundaries() -> None:
    y = _labels()
    policy = _policy(mode="non_inferiority", margin=1.0,
                     metric_guards={"precision": 0.25, "recall": 0.0, "rmse": 0.5,
                                    "no_such_metric": 1.0, "mae": 0.0})
    current = _scored(y, metrics={"precision": 0.75, "recall": 0.5, "rmse": 1.0, "mae": 1.0})
    candidate = _scored(y, metrics={"precision": 0.5, "recall": 0.5, "rmse": 1.5})
    d = decide(policy, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(), y=pd.Series(y),
               current=current, candidate=candidate)
    precision = _guard(d, "metric:precision")
    assert precision.passed and precision.evaluated  # a drop of exactly the allowed 0.25
    assert _guard(d, "metric:recall").passed
    assert _guard(d, "metric:rmse").passed  # lower is better: 1.0 -> 1.5 is a drop of 0.5
    unknown = _guard(d, "metric:no_such_metric")
    assert not unknown.passed and not unknown.evaluated
    missing = _guard(d, "metric:mae")
    assert missing.passed and not missing.evaluated
    tighter = _policy(mode="non_inferiority", margin=1.0, metric_guards={"rmse": 0.25})
    assert not _guard(decide(tighter, task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
                             y=pd.Series(y), current=current, candidate=candidate),
                      "metric:rmse").passed
    assert d.verdict == "REJECT" and len(d.reasons) == 1, d.reasons


def kill_reported_metric_values_take_precedence() -> None:
    y = _labels()
    d = decide(_policy(), task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(),
               y=pd.Series(y), current=_scored(y, metrics={"accuracy": 0.5}),
               candidate=_scored(y, metrics={"accuracy": 0.75}))
    assert (d.current_value, d.candidate_value, d.delta) == (0.5, 0.75, 0.25), d


# ---- golden decisions ----------------------------------------------------------------------------
def _clusters() -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(4)
    X = np.vstack([rng.normal(-2, 0.5, (40, 2)), rng.normal(2, 0.5, (40, 2))])
    return pd.DataFrame(X, columns=["a", "b"]), np.array([0] * 40 + [1] * 40)


def _scenarios() -> dict[str, GateDecision]:
    y = _labels()
    cur, better = _flip(y, range(16)), _flip(y, range(4))
    cells = pd.DataFrame({"cell": np.array(["A", "B"] * (N // 2))})
    thirds = pd.DataFrame({"cell": np.array(["A", "B", "C"] * N)[:N]})
    X_c, good = _clusters()
    shuffled = good.copy()
    shuffled[::3] = 1 - shuffled[::3]
    rng = np.random.default_rng(5)
    reg_y = rng.normal(size=N)
    prob_cur, prob_cand = rng.uniform(0, 1, N), np.clip(rng.uniform(-0.2, 1.2, N), 0, 1)
    return {
        "accuracy_superiority": _accuracy(_policy(margin=0.02), cur, better),
        # 130 resamples in batches of 50: the last batch is short.
        "accuracy_subsampled": _accuracy(_policy(max_rows=30, resamples=130), cur, better),
        # max_rows == the row count: every row is used, none subsampled.
        "accuracy_all_rows": _accuracy(_policy(max_rows=N), cur, better),
        "accuracy_slices_pass": _accuracy(
            _policy(mode="non_inferiority", margin=0.5,
                    slices={"columns": ["cell"], "min_rows": 10, "max_drop": 0.5}),
            better, cur, context=thirds),
        "accuracy_slices_relative": _accuracy(
            _policy(mode="non_inferiority", margin=0.5,
                    slices={"columns": ["cell"], "min_rows": 10, "max_drop": 0.1,
                            "relative": True}),
            cur, _flip(y, range(30)), context=thirds),
        "accuracy_slices": _accuracy(_policy(slices={"columns": ["cell"], "min_rows": 10,
                                                     "max_drop": 0.1}), better, cur,
                                     context=cells),
        "rmse": decide(_policy(margin=0.1, margin_relative=True), task=TaskType.REGRESSION,
                       metric="rmse", X=_X(), y=pd.Series(reg_y),
                       current=_scored(reg_y + rng.normal(0, 0.8, N)),
                       candidate=_scored(reg_y + rng.normal(0, 0.3, N))),
        "silhouette_pairwise": decide(
            _policy(pairwise_max_rows=30, slices={"columns": ["cell"]}),
            task=TaskType.CLUSTERING, metric="silhouette", X=X_c, y=None,
            current=_scored(shuffled), candidate=_scored(good), context=cells),
        "calibration": decide(
            _policy(mode="non_inferiority", margin=0.05, calibration={"enabled": True,
                                                                      "bins": 5}),
            task=TaskType.CLASSIFICATION, metric="accuracy", X=_X(), y=pd.Series(y),
            current=_scored(y, score=prob_cur), candidate=_scored(cur, score=prob_cand)),
    }


def _rounded(value: object) -> object:
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def snapshot() -> dict[str, object]:
    return {name: _rounded(d.model_dump(mode="json")) for name, d in _scenarios().items()}


def write_golden() -> None:
    GOLDEN.write_text(json.dumps(snapshot(), indent=2, sort_keys=True) + "\n",
                      encoding="utf-8", newline="\n")


def kill_golden_decisions() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(snapshot()))
    for name in expected:
        assert got[name] == expected[name], name
    assert set(got) == set(expected)
