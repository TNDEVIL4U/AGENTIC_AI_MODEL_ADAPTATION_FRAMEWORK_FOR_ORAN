"""Hardening Phase 13: the scenario matrix (scenario_matrix.py) is complete, and the scenarios
no earlier test covered: a stale version on a fast path, a large dataset read in bounded
memory, and each outbound dependency unavailable in turn."""

from __future__ import annotations

import ast
import socket
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest
import scenario_matrix

from oran_adapt.adapters.datasets import FileDataset, HttpDataset
from oran_adapt.adapters.deployment._common import HttpApi, StaticToken
from oran_adapt.adapters.deployment.webhook import WebhookDeployment
from oran_adapt.adapters.jwt import JwksCache
from oran_adapt.adapters.notify import WebhookSink
from oran_adapt.adapters.vault import VaultSecrets
from oran_adapt.core.errors import (
    AuthenticationError,
    ConfigurationError,
    DataSourceUnavailableError,
    DeploymentUnavailableError,
    ModelNotFoundError,
    NotificationDeliveryError,
)
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.datastore.access import DataAccess
from oran_adapt.datastore.versioning import version_row
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import ModelMetadata
from oran_adapt.orchestrator.pipeline import run_adaptation_job

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 1, tzinfo=UTC)


# ---- the matrix itself ----------------------------------------------------------------------
def _defined(rel: str) -> set[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def test_every_scenario_in_the_spec_is_mapped_to_tests_that_exist() -> None:
    assert set(scenario_matrix.SCENARIOS) == set(scenario_matrix.REQUIRED)
    for scenario, cases in scenario_matrix.SCENARIOS.items():
        assert cases, f"{scenario} has no test"
        for node_id in cases:
            rel, func = node_id.split("::")
            assert func.split("[")[0] in _defined(rel), f"{scenario}: {node_id} does not exist"


def test_every_scenario_has_a_fast_test() -> None:
    """Heavy tests may add depth; each scenario still runs in the non-heavy tier."""
    for scenario, cases in scenario_matrix.SCENARIOS.items():
        fast = [c for c in cases if not scenario_matrix.is_heavy(ROOT, c)]
        assert fast, f"{scenario}: every test is marked heavy"


def test_every_dependency_is_taken_down_in_turn() -> None:
    covered = set(scenario_matrix.DEPENDENCIES)
    assert covered == set(scenario_matrix.REQUIRED_DEPENDENCIES)
    for dependency, node_id in scenario_matrix.DEPENDENCIES.items():
        assert node_id in scenario_matrix.SCENARIOS["dependency unavailable"], dependency


# ---- stale version --------------------------------------------------------------------------
class _LiveIs:
    """A registry that only answers which version is LIVE."""

    def __init__(self, version: str) -> None:
        self.version = version

    def get_version_by_alias(self, name: str, alias: str) -> str:
        if name != "kpi-model":
            raise ModelNotFoundError(f"no model {name}")
        return self.version


def test_a_drift_event_on_a_version_that_is_no_longer_live_takes_no_action(
    migrated_settings, tmp_path
) -> None:
    session_factory = make_session_factory(create_db_engine(migrated_settings.database_url))
    with session_scope(session_factory) as s:
        s.add(ModelMetadata(model_id="cell-a", mlflow_model_name="kpi-model"))
    with session_scope(session_factory) as s:
        result = run_adaptation_job(
            s, DriftEvent(model_id="cell-a", event_id="evt-old", model_version="3"),
            migrated_settings, registry=_LiveIs("4"), llm_client=None, workdir=str(tmp_path),
        )
    assert result.outcome == "NO_ACTION"
    assert result.live_version == "4"
    assert "stale" in (result.reason or "")


# ---- large dataset --------------------------------------------------------------------------
LARGE_ROWS = 100_000


def test_a_large_dataset_is_registered_and_sampled_in_bounded_memory(
    migrated_settings, tmp_path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "large.csv"
    for start in range(0, LARGE_ROWS, 25_000):  # written in chunks too
        index = range(start, start + 25_000)
        pd.DataFrame({
            "ts": [(T0 + timedelta(seconds=i)).isoformat() for i in index],
            "cell": [f"c{i % 7}" for i in index],
            "prb_util": [(i % 100) / 100 for i in index],
            "users": list(index),
        }).to_csv(path, mode="a", header=start == 0, index=False)
    access = DataAccess([FileDataset([str(data_dir)])], chunk_rows=5_000,
                        analysis_max_rows=1_000)
    session_factory = make_session_factory(create_db_engine(migrated_settings.database_url))
    tracemalloc.start()
    try:
        with session_scope(session_factory) as s:
            access.register(s, "kpi", "v-large", path.as_uri(), kind="HISTORICAL",
                            timestamp_column="ts")
            dv = version_row(s, "kpi", "v-large")
            rows = access.sample(s, dv)
            total = dv.row_count
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == LARGE_ROWS
    assert len(rows) == 1_000
    users = [r.payload["users"] for r in rows]
    assert users[0] == 0 and users[-1] >= LARGE_ROWS - LARGE_ROWS // 1_000  # spread to the end
    # Reading the whole file at once is the baseline; the chunked path holds far less.
    tracemalloc.start()
    try:
        whole = pd.read_csv(path)
        _, whole_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(whole) == LARGE_ROWS
    assert peak < whole_peak / 2, f"chunked peak {peak} bytes, whole-file read {whole_peak}"


# ---- each dependency unavailable in turn ----------------------------------------------------
@pytest.fixture
def closed_port() -> int:
    """A localhost port nothing listens on (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _local_policy() -> OutboundPolicy:
    return OutboundPolicy(["127.0.0.1"])


def _http_dataset(base: str) -> None:
    HttpDataset(["127.0.0.1"], allow_plain=True, client=httpx.Client(timeout=2),
                max_bytes=1024).stat(f"{base}/kpi.csv")


def _serving_system(base: str) -> None:
    api = HttpApi(base, service="webhook",
                  http_factory=lambda: _local_policy().client(timeout=2),
                  token=StaticToken(None))
    WebhookDeployment(api).status("cell-a")


def _notification_webhook(base: str) -> None:
    WebhookSink(f"{base}/hook", _local_policy().client(timeout=2), 2).send(
        type("Msg", (), {"body": b"{}", "headers": {}})()  # the sink only reads these two
    )


def _notification_ping(base: str) -> None:
    WebhookSink(f"{base}/hook", _local_policy().client(timeout=2), 2).ping()


def _vault(base: str) -> None:
    VaultSecrets(base, mount="secret", path="oran", token="t", namespace=None,
                 policy=_local_policy(), timeout_s=2).get("database_url")


def _jwks(base: str) -> None:
    JwksCache(f"{base}/jwks", lambda: _local_policy().client(timeout=2), cache_ttl_s=60,
              min_refetch_s=1).keys(None)


OUTAGES = {
    "http-dataset": (_http_dataset, DataSourceUnavailableError),
    "serving-system": (_serving_system, DeploymentUnavailableError),
    "notification-webhook": (_notification_webhook, NotificationDeliveryError),
    "notification-ping": (_notification_ping, NotificationDeliveryError),
    "vault": (_vault, ConfigurationError),
    "oidc-jwks": (_jwks, AuthenticationError),
}


@pytest.mark.parametrize("dependency", sorted(OUTAGES))
def test_each_dependency_unavailable_in_turn_fails_with_its_typed_error(
    dependency, closed_port
) -> None:
    call, expected = OUTAGES[dependency]
    with pytest.raises(expected) as caught:
        call(f"http://127.0.0.1:{closed_port}")
    error = caught.value
    assert not isinstance(error, httpx.HTTPError | OSError)  # never the raw transport error
    if isinstance(error, NotificationDeliveryError):
        assert error.retryable, "an unreachable sink is retried, not dead-lettered"
