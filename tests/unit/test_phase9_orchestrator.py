"""Phase 9: the orchestrator - analyze() -> decide() -> engine/LLM-fallback -> validate ->
register, run end to end against a real SQLite DB, a real file/SQLite-backed MLflow, and real
sklearn fits. The LLM provider boundary uses the same hand-written FakeLlmClient pattern as
test_phase7_sandbox.py, since it is the one genuinely external collaborator.

Three scenarios exercise the three distinct outcomes the exit gate calls for:
  1. no drift reported -> REUSE -> NO_ACTION, nothing touched.
  2. a large, genuine shift in a non-predictive feature -> FULL_RETRAINING via the built-in
     sklearn engine -> validated -> REGISTERED as a new MLflow version.
  3. a smaller shift -> FINE_TUNING is still compatible, but the current model only has
     warm_start (no partial_fit), so the built-in engine raises UnsupportedAdaptationError and
     the orchestrator falls back to the LLM/sandbox adapter -> REGISTERED.

The label is deliberately derived only from `rsrp` (label = rsrp > -90), which is never the
feature we drift. That decouples "how much prb_util shifted" from "how accurate the model stays",
so validation passes deterministically instead of depending on how a classifier reacts to an
unrelated feature's distribution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mlflow
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from oran_adapt.core.enums import AssociationRole, DataKind, EngineKind, Strategy
from oran_adapt.core.errors import ModelNotFoundError
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import (
    DataRecord,
    DatasetMetadata,
    DataVersion,
    ModelDataAssociation,
    ModelMetadata,
)
from oran_adapt.orchestrator.pipeline import run_adaptation_job
from oran_adapt.registry.client import MlflowRegistry

FEATURES = ["prb_util", "rsrp"]
TARGET = "label"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

_LLM_CODE = (
    "from sklearn.linear_model import LogisticRegression\n"
    "def adapt(current_model, X, y):\n"
    "    model = LogisticRegression()\n"
    "    model.fit(X, y)\n"
    "    return model\n"
)


class FakeLlmClient:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return self._response


def _frame(n: int, *, prb_lo: float, prb_hi: float, prb_seed: int, rsrp_seed: int) -> pd.DataFrame:
    prb = np.random.default_rng(prb_seed).uniform(prb_lo, prb_hi, size=n)
    rsrp = np.random.default_rng(rsrp_seed).uniform(-120, -60, size=n)
    label = (rsrp > -90).astype(int)
    return pd.DataFrame({"prb_util": prb, "rsrp": rsrp, "label": label})


@pytest.fixture
def session_factory(migrated_settings):
    engine = create_db_engine(migrated_settings.database_url)
    return make_session_factory(engine)


@pytest.fixture
def registry(settings) -> MlflowRegistry:
    # mlflow.*.log_model (used by _seed_model below) only follows the *global* fluent tracking/
    # registry URI, which a previous test in this session may have left pointed elsewhere (e.g.
    # via MlflowRegistry.register_candidate) - reset both explicitly so each test starts clean.
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
    return MlflowRegistry(settings.mlflow_tracking_uri)


def _insert_version(session, dataset, *, version, kind, role, model_id, frame, start):
    dv = DataVersion(
        dataset_id=dataset.id,
        version=version,
        kind=kind,
        data_start=start,
        data_end=start + timedelta(days=len(frame)),
        row_count=len(frame),
    )
    session.add(dv)
    session.flush()
    for i, row in enumerate(frame.to_dict(orient="records")):
        session.add(
            DataRecord(data_version_id=dv.id, observed_at=start + timedelta(days=i), payload=row)
        )
    session.add(
        ModelDataAssociation(
            model_id=model_id, model_version="1", data_version_id=dv.id, role=role
        )
    )


def _seed_model(
    session_factory,
    registry: MlflowRegistry,
    settings,
    *,
    model_id: str,
    mlflow_name: str,
    historical: pd.DataFrame,
    drifted: pd.DataFrame,
    warm_start: bool,
) -> None:
    clf = LogisticRegression(warm_start=warm_start).fit(historical[FEATURES], historical[TARGET])
    with mlflow.start_run():
        mlflow.sklearn.log_model(clf, name="model", registered_model_name=mlflow_name)
    registry.set_alias(mlflow_name, settings.live_alias, "1")

    with session_scope(session_factory) as session:
        session.add(
            ModelMetadata(
                model_id=model_id,
                mlflow_model_name=mlflow_name,
                model_type="classification",
                framework="sklearn",
                task_type="classification",
                target_column=TARGET,
            )
        )
        dataset = DatasetMetadata(dataset_id=f"{model_id}-ds", name=model_id, schema={})
        session.add(dataset)
        session.flush()
        _insert_version(
            session,
            dataset,
            version="hist-1",
            kind=DataKind.HISTORICAL,
            role=AssociationRole.TRAINING,
            model_id=model_id,
            frame=historical,
            start=T0,
        )
        _insert_version(
            session,
            dataset,
            version="drift-1",
            kind=DataKind.DRIFTED,
            role=AssociationRole.DRIFT_OBSERVED,
            model_id=model_id,
            frame=drifted,
            start=T0 + timedelta(days=100),
        )


# ---- scenario 1: no drift reported -> REUSE -> NO_ACTION -----------------------------------
def test_orchestrator_reuse_results_in_no_action(session_factory, registry, migrated_settings, tmp_path) -> None:
    historical = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=2, rsrp_seed=11)
    _seed_model(
        session_factory,
        registry,
        migrated_settings,
        model_id="reuse-model",
        mlflow_name="reuse_model_mlflow",
        historical=historical,
        drifted=drifted,
        warm_start=False,
    )

    with session_scope(session_factory) as session:
        event = DriftEvent(model_id="reuse-model", drift_detected=False)
        result = run_adaptation_job(
            session,
            event,
            migrated_settings,
            registry=registry,
            llm_client=None,
            workdir=str(tmp_path / "work"),
        )

    assert result.outcome == "NO_ACTION"
    assert result.strategy is None
    assert result.candidate is None
    assert registry.get_version_by_alias("reuse_model_mlflow", migrated_settings.live_alias) == "1"


# ---- scenario 2: large drift in a non-predictive feature -> FULL_RETRAINING -> REGISTERED ----
def test_orchestrator_full_retrain_registers_new_version(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    historical = _frame(60, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(60, prb_lo=5.0, prb_hi=6.0, prb_seed=3, rsrp_seed=11)
    _seed_model(
        session_factory,
        registry,
        migrated_settings,
        model_id="retrain-model",
        mlflow_name="retrain_model_mlflow",
        historical=historical,
        drifted=drifted,
        warm_start=False,
    )

    with session_scope(session_factory) as session:
        event = DriftEvent(model_id="retrain-model", drift_detected=True)
        result = run_adaptation_job(
            session,
            event,
            migrated_settings,
            registry=registry,
            llm_client=None,
            workdir=str(tmp_path / "work"),
        )

    assert result.outcome == "REGISTERED", result.reason
    assert result.strategy == Strategy.FULL_RETRAINING
    assert result.candidate is not None
    assert result.candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert result.validation is not None and result.validation.passed is True
    assert result.registered_version == "2"
    assert registry.get_version_by_alias("retrain_model_mlflow", migrated_settings.live_alias) == "2"


# ---- scenario 3: smaller drift -> FINE_TUNING selected, but the artifact only has warm_start
# (no partial_fit) -> the built-in engine can't do it -> falls back to the LLM/sandbox adapter ---
def test_orchestrator_llm_sandbox_fallback_registers_new_version(
    session_factory, registry, migrated_settings, tmp_path
) -> None:
    historical = _frame(60, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(60, prb_lo=0.1, prb_hi=1.1, prb_seed=2, rsrp_seed=11)
    _seed_model(
        session_factory,
        registry,
        migrated_settings,
        model_id="fallback-model",
        mlflow_name="fallback_model_mlflow",
        historical=historical,
        drifted=drifted,
        warm_start=True,
    )

    llm_client = FakeLlmClient(response=_LLM_CODE)
    with session_scope(session_factory) as session:
        event = DriftEvent(model_id="fallback-model", drift_detected=True)
        result = run_adaptation_job(
            session,
            event,
            migrated_settings,
            registry=registry,
            llm_client=llm_client,
            workdir=str(tmp_path / "work"),
        )

    assert result.outcome == "REGISTERED", result.reason
    assert result.strategy == Strategy.FINE_TUNING
    assert result.candidate is not None
    assert result.candidate.engine == EngineKind.LLM_GENERATED
    assert llm_client.calls  # the LLM was actually invoked, not skipped
    assert result.validation is not None and result.validation.passed is True
    assert result.registered_version == "2"
    assert registry.get_version_by_alias("fallback_model_mlflow", migrated_settings.live_alias) == "2"


# ---- guard rail: an unknown model_id is a precondition failure, not a JobResult outcome ------
def test_orchestrator_propagates_model_not_found(session_factory, registry, migrated_settings, tmp_path) -> None:
    with session_scope(session_factory) as session, pytest.raises(ModelNotFoundError):
        run_adaptation_job(
            session,
            DriftEvent(model_id="does-not-exist", drift_detected=True),
            migrated_settings,
            registry=registry,
            llm_client=None,
            workdir=str(tmp_path / "work"),
        )
