"""Shared fixtures. Real SQLite DB + real file/SQLite-backed MLflow in temp dirs (no mocks)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import skip_policy
from oran_adapt.api.app import create_app
from oran_adapt.core.config import Settings
from oran_adapt.core.policies import GatePolicy
from oran_adapt.db.migrate import upgrade_to_head

# The suite drives the API far faster than any real caller: per-replica rate limits are off
# unless a test sets them, and the outbound policy does not resolve names (hosts in tests are
# placeholders; the SSRF tests resolve through a patched resolver).
for _key, _value in (("API_RATE_LIMIT_PER_MINUTE", "0"),
                     ("API_AUTH_FAILURE_LIMIT_PER_MINUTE", "0"),
                     ("OUTBOUND_RESOLVE_HOSTS", "false")):
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _isolate_mlflow_global_uris(monkeypatch) -> Iterator[None]:
    """Several tests point MLflow's process-global tracking/registry URIs (and the MLFLOW_*_URI
    env vars the setters write) at their own temp store. Reset both after every test so a later
    test's fluent ``log_model`` can't silently register into an earlier test's store."""
    from mlflow.tracking import _model_registry, _tracking_service

    for key in ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI"):
        monkeypatch.delenv(key, raising=False)
    yield
    _tracking_service.utils._tracking_uri = None
    _model_registry.utils._registry_uri = None
    for key in ("MLFLOW_TRACKING_URI", "MLFLOW_REGISTRY_URI"):
        os.environ.pop(key, None)  # monkeypatch then restores any value the shell had set


@pytest.fixture
def database_url(tmp_path) -> str:
    # Set TEST_DATABASE_URL to a PostgreSQL URL to run the suite against real PostgreSQL.
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'app.db').as_posix()}"


@pytest.fixture
def settings(tmp_path, database_url) -> Settings:
    art = tmp_path / "mlartifacts"
    art.mkdir()
    return Settings(
        _env_file=None,
        database_url=database_url,
        mlflow_tracking_uri=f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp_path / "work"),
        log_json=False,
        # Most tests exercise behaviour, not access control; test_phase14_stage_b turns it on.
        auth_enabled=False,
        # No background dispatcher thread in every app a test starts; the notification tests
        # (test_phase4_notifications) turn it on or drive the dispatcher themselves.
        notification_dispatch_enabled=False,
        # A submitted job runs in the submitting call, so tests see its outcome at once; the
        # Phase 6 tests (test_phase6_execution) drive the queue and workers themselves.
        job_queue_backend="inline",
        # Pre-Phase 7 behaviour for the tests that exercise the pipeline, not delivery: LIVE moves
        # at once, and a candidate as good as the incumbent passes (non-inferiority within 5 %).
        # test_phase7_gate_delivery covers the superiority gate and the rollout strategies.
        delivery_strategy="blue_green",
        gate_policy=GatePolicy(mode="non_inferiority", margin=0.05, margin_relative=True),
    )


@pytest.fixture
def migrated_settings(settings) -> Settings:
    upgrade_to_head(settings.database_url)
    return settings


@pytest.fixture
def client(migrated_settings) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_settings)) as c:
        yield c


def _enforce_skip_policy(report) -> None:
    """An untagged or expired skip or xfail fails (tests/skip_policy.py)."""
    if not report.skipped:
        return
    reason = getattr(report, "wasxfail", None)
    if reason is None and isinstance(report.longrepr, tuple):
        reason = str(report.longrepr[2])
    broken = skip_policy.problem(reason, datetime.now(UTC).date())
    if broken is not None:
        report.outcome = "failed"
        report.longrepr = f"skip policy: the skip reason {reason!r} {broken}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failing sandbox run keeps the child's stderr in the error's context, which pytest never
    prints. Append its tail to the one-line failure summary so CI annotations show the cause."""
    outcome = yield
    report = outcome.get_result()
    _enforce_skip_policy(report)
    excinfo = call.excinfo
    if not report.failed or excinfo is None:
        return
    context = getattr(excinfo.value, "context", None)
    stderr = context.get("stderr") if isinstance(context, dict) else None
    crash = getattr(report.longrepr, "reprcrash", None)
    if stderr and crash is not None:
        tail = " | ".join(line.strip() for line in str(stderr).splitlines()[-8:] if line.strip())
        crash.message += f" [returncode={context.get('returncode')}; stderr: {tail[-800:]}]"
