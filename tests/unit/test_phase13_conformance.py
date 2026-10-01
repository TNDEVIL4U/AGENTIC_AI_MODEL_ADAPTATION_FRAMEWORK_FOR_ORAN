"""Hardening Phase 13: conformance suites for the ports that had none (artifact_store,
model_handler, cdc_source, job_executor, policy), run on every installed adapter, and the gate
that stops an adapter from being registered without passing its port's suite."""

from __future__ import annotations

import ast
import importlib
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from conformance_coverage import COVERAGE, EXEMPT, SUITES, case_matches, covering
from sklearn.linear_model import LogisticRegression

from oran_adapt import plugins
from oran_adapt.adapters.access import StaticRbacPolicy
from oran_adapt.adapters.handlers.native import NativeHandler
from oran_adapt.adapters.job_executors import ProcessJobExecutor, ThreadJobExecutor
from oran_adapt.adapters.kafka_cdc import KafkaCdcSource
from oran_adapt.adapters.registry.artifact_stores import (
    FilesystemArtifactStore,
    FsspecArtifactStore,
)
from oran_adapt.adapters.registry.mlflow.flavors import MlflowFlavorHandler
from oran_adapt.bootstrap import build_cdc_source
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance import artifact_store as store_suite
from oran_adapt.conformance import cdc_source as cdc_suite
from oran_adapt.conformance import job_executor as executor_suite
from oran_adapt.conformance import model_handler as handler_suite
from oran_adapt.conformance import policy as policy_suite
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import PermissionDeniedError
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import KpiSample

HERE = Path(__file__).parent
T0 = datetime(2026, 3, 1, tzinfo=UTC)


# ---- artifact_store ------------------------------------------------------------------------------
def _artifact_stores(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the store root should be")
    return {
        "filesystem": (FilesystemArtifactStore(str(tmp_path / "store")),
                       lambda: FilesystemArtifactStore(str(blocker / "root"))),
        "fsspec": (FsspecArtifactStore((tmp_path / "fsspec").as_uri()),
                   lambda: FsspecArtifactStore((blocker / "root").as_uri())),
    }


@pytest.mark.parametrize("adapter", ["filesystem", "fsspec"])
def test_artifact_store_conformance(adapter, tmp_path) -> None:
    store, broken = _artifact_stores(tmp_path)[adapter]
    ctx = store_suite.Context(workdir=str(tmp_path / "work"), broken=broken)
    assert store_suite.run(store, ctx) == list(store_suite.CHECKS)


class _OverwritingStore:
    """A store that lets a second put replace the first: write-once broken."""

    def __init__(self, root: str) -> None:
        self.inner = FilesystemArtifactStore(root)

    def put(self, key, local_path):
        if self.inner.exists(key):
            return key  # the defect: a second put is silently accepted
        return self.inner.put(key, local_path)

    def get(self, key, dst_dir):
        return self.inner.get(key, dst_dir)

    def exists(self, key):
        return self.inner.exists(key)


def test_artifact_store_suite_catches_a_store_that_overwrites(tmp_path) -> None:
    ctx = store_suite.Context(workdir=str(tmp_path / "work"))
    bad = _OverwritingStore(str(tmp_path / "store"))
    with pytest.raises(ConformanceFailure, match="existing key"):
        store_suite.CHECKS["write_once"](bad, ctx)


# ---- model_handler -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fitted():
    rng = np.random.default_rng(13)
    X = rng.normal(size=(60, 3))
    y = (X[:, 0] + 0.3 * X[:, 1] > 0).astype(int)
    return LogisticRegression().fit(X, y), X


_HANDLERS = {
    "native": lambda: NativeHandler(skops_trusted_types=()),
    "mlflow-flavors": lambda: MlflowFlavorHandler(skops_trusted_types=(),
                                                  pip_requirements=["scikit-learn"]),
}


@pytest.mark.parametrize("adapter", sorted(_HANDLERS))
def test_model_handler_conformance(adapter, fitted, tmp_path) -> None:
    model, X = fitted
    ctx = handler_suite.Context(workdir=str(tmp_path), model=model, framework="sklearn", X=X,
                                predict=lambda m, rows: m.predict_proba(rows))
    assert handler_suite.run(_HANDLERS[adapter](), ctx) == list(handler_suite.CHECKS)


def test_native_handler_refuses_an_existing_directory_with_a_typed_error(fitted, tmp_path):
    """Before Phase 13 an existing destination raised a bare FileExistsError."""
    from oran_adapt.core.errors import ArtifactError

    (tmp_path / "exists").mkdir()
    with pytest.raises(ArtifactError, match="existing directory"):
        NativeHandler(skops_trusted_types=()).save(fitted[0], "sklearn", str(tmp_path / "exists"))


class _ForgetfulHandler:
    """Saves fine, but loads a fresh, unfitted model: roundtrip broken."""

    def __init__(self) -> None:
        self.inner = NativeHandler(skops_trusted_types=())
        self.frameworks = self.inner.frameworks
        self.format = self.inner.format

    def detect(self, local_path):
        return self.inner.detect(local_path)

    def save(self, model, framework, dst_dir):
        return self.inner.save(model, framework, dst_dir)

    def load(self, local_path, framework):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(20, 3))
        return LogisticRegression().fit(X, (X[:, 2] > 0).astype(int))


def test_model_handler_suite_catches_a_lossy_roundtrip(fitted, tmp_path) -> None:
    model, X = fitted
    ctx = handler_suite.Context(workdir=str(tmp_path), model=model, framework="sklearn", X=X,
                                predict=lambda m, rows: m.predict_proba(rows))
    with pytest.raises(ConformanceFailure, match="predict what the saved model predicted"):
        handler_suite.CHECKS["roundtrip"](_ForgetfulHandler(), ctx)


# ---- cdc_source ----------------------------------------------------------------------------------
class _Broker:
    """One Kafka topic partition holding Debezium envelopes, with committed offsets per group."""

    topic = "oran.public.kpi_sample"

    def __init__(self) -> None:
        self.log: list[bytes] = []
        self.committed: dict[str, int] = {}
        self.down = False
        self._ids = itertools.count(1)

    def emit(self, n: int) -> None:
        for _ in range(n):
            i = next(self._ids)
            row = {"id": i, "dataset_id": "kpi", "observed_at": "2026-03-01T00:00:00Z",
                   "payload": json.dumps({"prb_util": 0.1 * i, "rsrp": -90.0, "label": 1})}
            payload = {"before": None, "after": row, "op": "c", "ts_ms": 1_767_225_600_500,
                       "source": {"version": "2.7.0.Final", "connector": "postgresql",
                                  "table": "kpi_sample", "txId": 1000 + i, "lsn": i,
                                  "ts_ms": 1_767_225_600_000}}
            self.log.append(json.dumps({"schema": {}, "payload": payload}).encode())


class _BrokerMsg:
    def __init__(self, value: bytes, offset: int) -> None:
        self._value, self._offset = value, offset

    def value(self):
        return self._value

    def topic(self):
        return _Broker.topic

    def partition(self):
        return 0

    def offset(self):
        return self._offset

    def error(self):
        return None


class _BrokerConsumer:
    """A group member: starts at the group's committed offset; commit stores its position."""

    def __init__(self, broker: _Broker, group: str) -> None:
        self.broker, self.group = broker, group
        self.position = broker.committed.get(group, 0)

    def subscribe(self, topics):
        assert topics == [self.broker.topic]

    def consume(self, num_messages, timeout):
        if self.broker.down:
            raise ConnectionError("broker unreachable")
        start = self.position
        batch = self.broker.log[start:start + num_messages]
        self.position = start + len(batch)
        return [_BrokerMsg(value, start + n) for n, value in enumerate(batch)]

    def commit(self, asynchronous=True):
        self.broker.committed[self.group] = self.position

    def close(self):
        return None


def _cdc_context(adapter, migrated_settings, kafka_cls=KafkaCdcSource):
    session_factory = make_session_factory(create_db_engine(migrated_settings.database_url))
    if adapter == "kafka":
        settings = migrated_settings.model_copy(update={"cdc_kafka_topic": _Broker.topic,
                                                        "cdc_consumer_group": "oran-adapt-cdc"})
        broker = _Broker()

        def open_kafka():
            return kafka_cls(settings, _BrokerConsumer(broker, settings.cdc_consumer_group))

        def break_kafka():
            broker.down = True

        ctx = cdc_suite.Context(session_factory=session_factory, emit=broker.emit,
                                reopen=open_kafka, break_source=break_kafka)
        return open_kafka(), ctx
    settings = migrated_settings.model_copy(update={"cdc_mode": "polling"})
    ids = itertools.count(1)

    def insert(n: int) -> None:
        with session_scope(session_factory) as s:
            for _ in range(n):
                i = next(ids)
                s.add(KpiSample(id=i, dataset_id="kpi", observed_at=T0,
                                payload={"prb_util": 0.1 * i, "rsrp": -90.0, "label": 1}))

    ctx = cdc_suite.Context(session_factory=session_factory, emit=insert,
                            reopen=lambda: build_cdc_source(settings))
    return build_cdc_source(settings), ctx


@pytest.mark.parametrize("adapter", ["polling", "kafka"])
def test_cdc_source_conformance(adapter, migrated_settings) -> None:
    source, ctx = _cdc_context(adapter, migrated_settings)
    assert cdc_suite.run(source, ctx) == list(cdc_suite.CHECKS)


class _EagerCommit(KafkaCdcSource):
    """Commits on fetch instead of on ack: a worker dying before storing loses the batch."""

    def fetch(self, session, limit):
        out = super().fetch(session, limit)
        self.ack()
        return out


def test_cdc_source_suite_catches_a_source_that_commits_before_storage(migrated_settings):
    source, ctx = _cdc_context("kafka", migrated_settings, kafka_cls=_EagerCommit)
    with pytest.raises(ConformanceFailure, match="delivered again after a restart"):
        cdc_suite.CHECKS["unacknowledged_redelivered"](source, ctx)


# ---- job_executor --------------------------------------------------------------------------------
_EXECUTORS = {
    "thread": ThreadJobExecutor,
    "process": lambda: ProcessJobExecutor(kill_grace_s=2.0),
}


@pytest.mark.parametrize("adapter", sorted(_EXECUTORS))
def test_job_executor_conformance(adapter) -> None:
    assert executor_suite.run(_EXECUTORS[adapter](), executor_suite.Context()) == list(
        executor_suite.CHECKS)


class _SwallowingExecutor:
    """Returns the crash as a result instead of raising it."""

    in_process = True

    def execute(self, call):
        try:
            return call.run(call.payload)
        except Exception as exc:  # noqa: BLE001 - the defect under test
            return {"error": str(exc)}


def test_job_executor_suite_catches_a_swallowed_crash() -> None:
    with pytest.raises(ConformanceFailure, match="must raise"):
        executor_suite.CHECKS["crash_raised"](_SwallowingExecutor(), executor_suite.Context())


# ---- policy --------------------------------------------------------------------------------------
def test_policy_conformance(settings) -> None:
    policy = plugins.adapters("policy")["static-rbac"].factory(settings)
    assert policy_suite.run(policy, policy_suite.Context()) == list(policy_suite.CHECKS)
    assert policy_suite.run(StaticRbacPolicy(settings.policy_roles),
                            policy_suite.Context()) == list(policy_suite.CHECKS)


class _AllowByDefault:
    def allowed_roles(self, action):
        return frozenset(Role) if action not in ("admin",) else frozenset({Role.ADMIN})

    def authorize(self, principal, action):
        if principal.role not in self.allowed_roles(action):
            raise PermissionDeniedError(f"{principal.role} may not {action}")


def test_policy_suite_catches_allow_by_default() -> None:
    with pytest.raises(ConformanceFailure, match="unconfigured action"):
        policy_suite.CHECKS["deny_by_default"](_AllowByDefault(), policy_suite.Context())


# ---- the registration gate -----------------------------------------------------------------------
def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_every_installed_adapter_is_conformance_tested() -> None:
    """An entry point under ``oran_adapt.<port>`` with no conformance test (or an unexpired,
    owned exemption) fails here; so does a stale entry naming a test that no longer exists."""
    missing = []
    for port, module in SUITES.items():
        importlib.import_module(module)
        for adapter in plugins.adapters(port):
            if covering(port, adapter):
                continue
            exemption = EXEMPT.get((port, adapter))
            if exemption is None:
                missing.append(f"{port}/{adapter}")
            else:
                assert exemption.owner and exemption.reason, (port, adapter)
                assert exemption.expires >= datetime.now(UTC).date(), f"expired: {port}/{adapter}"
    assert not missing, f"adapters without a conformance test: {missing}"
    functions = {}
    for port, entries in COVERAGE.items():
        assert port in SUITES, port
        for path, func, _ in entries:
            if path not in functions:
                functions[path] = _test_functions(HERE / path)
            assert func in functions[path], f"{path}::{func} does not exist"


def test_the_gate_names_every_port_that_has_adapters() -> None:
    ports = {ep.group.removeprefix(plugins.GROUP_PREFIX) for ep in _all_entry_points()}
    assert ports <= set(SUITES), sorted(ports - set(SUITES))


def _all_entry_points():
    from importlib.metadata import entry_points

    return [ep for ep in entry_points() if ep.group.startswith(plugins.GROUP_PREFIX)]


def test_case_matching_selects_one_adapters_cases() -> None:
    assert case_matches("test_conformance[mirror-protocol]", "test_conformance", "mirror")
    assert case_matches("test_conformance[sagemaker-emulator-pickle]", "test_conformance",
                        "sagemaker-emulator")
    assert case_matches("test_conformance[email]", "test_conformance", "email")
    assert case_matches("test_policy_conformance", "test_policy_conformance", None)
    assert not case_matches("test_conformance_extra", "test_conformance", None)
