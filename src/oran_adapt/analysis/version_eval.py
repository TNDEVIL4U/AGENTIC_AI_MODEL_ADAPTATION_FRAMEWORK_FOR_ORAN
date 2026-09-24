"""Member 1 - historical version evaluation: score every registered version of a model on the
current data before anything is trained.

The current data is the held-out newest slice of the drifted data (the same rows the validation
gate uses later, never trained on). Versions are scored one at a time and dropped straight
after, so memory stays at one model regardless of how many versions exist. The newest
``reuse_max_versions`` READY versions are scored, plus LIVE if it is older than those.

A version is *compatible* when its artifact passes its checksum, loads, is the same kind of
estimator as LIVE (classifier vs regressor), and every feature it needs is in the data. An
incompatible version is reported with the reason, never silently skipped. An unreachable
MLflow is not a per-version problem: RegistryUnavailableError propagates so the job retries.
"""

from __future__ import annotations

import gc
import logging
import os
import time

import pandas as pd

from oran_adapt.adaptation.inspector import inspect_model
from oran_adapt.adaptation.loaders import load_native_model
from oran_adapt.analysis.schemas import VersionEvaluation
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import AdaptationError, RegistryUnavailableError
from oran_adapt.core.logging import log_event
from oran_adapt.registry.client import MlflowRegistry
from oran_adapt.registry.promotion import verify_version_artifact
from oran_adapt.validation.evaluate import evaluate_model
from oran_adapt.validation.metrics import higher_is_better

logger = logging.getLogger(__name__)


def _candidates(registry: MlflowRegistry, name: str, live_version: str, limit: int) -> list:
    ready = [v for v in registry.list_versions(name) if (v.status or "READY") == "READY"]
    newest = ready[-limit:]
    if all(str(v.version) != live_version for v in newest):
        newest += [v for v in ready if str(v.version) == live_version]
    return sorted(newest, key=lambda v: int(v.version))


def _degradation(metric: str, now: float, baseline: dict[str, float]) -> float | None:
    if metric not in baseline:
        return None
    # Positive means worse now: a higher-is-better metric fell, or an error metric rose.
    return baseline[metric] - now if higher_is_better(metric) else now - baseline[metric]


def _evaluate_one(
    registry: MlflowRegistry,
    name: str,
    version_info,
    *,
    framework: str,
    target_column: str,
    data: pd.DataFrame,
    live_version: str,
    expected_estimator: str | None,
    workdir: str,
    task_type: str | None = None,
) -> VersionEvaluation:
    version = str(version_info.version)
    ev = VersionEvaluation(version=version, is_live=version == live_version, n_rows=len(data))
    created_ms = getattr(version_info, "creation_timestamp", None)
    if created_ms:
        ev.age_days = round((time.time() * 1000 - created_ms) / 86_400_000, 4)
    ev.baseline_metrics = registry.get_run_metrics(getattr(version_info, "run_id", None))

    try:
        local, ev.artifact_sha256 = verify_version_artifact(registry, name, version, workdir)
        model = load_native_model(local, framework)
        inspection = inspect_model(model, framework)
    except RegistryUnavailableError:
        raise
    except AdaptationError as exc:
        ev.incompatibility_reason = f"{exc.code}: {exc.message}"
        return ev

    ev.estimator_type = inspection.estimator_type
    if expected_estimator and inspection.estimator_type != expected_estimator:
        ev.incompatibility_reason = (
            f"estimator type {inspection.estimator_type} differs from LIVE's {expected_estimator}"
        )
        return ev
    features = inspection.feature_names_in or [c for c in data.columns if c != target_column]
    missing = [f for f in features if f not in data.columns]
    if missing:
        ev.incompatibility_reason = f"features missing from current data: {missing}"
        return ev

    try:
        scores = evaluate_model(
            model,
            data[features],
            data[target_column],
            framework=framework,
            estimator_type=inspection.estimator_type,
            task_type=task_type,
        )
    except AdaptationError as exc:
        ev.incompatibility_reason = f"{exc.code}: {exc.message}"
        return ev
    finally:
        del model
        gc.collect()

    ev.metric_name, ev.metric_value = next(iter(scores.items()))
    ev.metrics = scores
    ev.degradation = _degradation(ev.metric_name, ev.metric_value, ev.baseline_metrics)
    ev.compatible = True
    return ev


def evaluate_versions(
    registry: MlflowRegistry,
    *,
    mlflow_name: str,
    framework: str,
    target_column: str,
    live_version: str,
    data: pd.DataFrame,
    settings: Settings,
    workdir: str,
    task_type: str | None = None,
) -> list[VersionEvaluation]:
    """Score the model's registered versions on ``data`` (which must hold ``target_column``).
    LIVE is scored first because its estimator type is what the others must match."""
    infos = _candidates(registry, mlflow_name, live_version, settings.reuse_max_versions)
    live_info = [v for v in infos if str(v.version) == live_version]
    others = [v for v in infos if str(v.version) != live_version]

    results: list[VersionEvaluation] = []
    expected: str | None = None
    for info in live_info + others:
        ev = _evaluate_one(
            registry,
            mlflow_name,
            info,
            framework=framework,
            target_column=target_column,
            data=data,
            live_version=live_version,
            expected_estimator=expected,
            workdir=os.path.join(workdir, "versions"),
            task_type=task_type,
        )
        if ev.is_live:
            expected = ev.estimator_type
        results.append(ev)
        log_event(
            logger,
            f"version {ev.version} scored: "
            + (
                f"{ev.metric_name}={ev.metric_value:.4f}"
                if ev.compatible
                else f"incompatible ({ev.incompatibility_reason})"
            ),
            model=mlflow_name,
            version=ev.version,
        )
    return sorted(results, key=lambda e: int(e.version))
