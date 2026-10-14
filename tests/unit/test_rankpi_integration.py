"""Data by database query (``sql`` and ``rankpi`` dataset adapters) and rollout health from the
rankpi ML lifecycle store (``rankpi`` rollout metrics adapter), against SQLite stand-ins that
carry the documented columns of the rankpi schema (docs/integration/rankpi.md)."""

from __future__ import annotations

import io
import json
import pickle
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from oran_adapt.adapters.datasets_sql import RANKPI_CUMULATIVE, RANKPI_DEAD, RunGuard, SqlDataset
from oran_adapt.adapters.rollout_metrics_rankpi import RankpiRolloutMetrics
from oran_adapt.conformance import dataset as dataset_conformance
from oran_adapt.conformance import rollout_metrics as rollout_conformance
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import (
    ConfigurationError,
    DatasetNotFoundError,
    DataSourceNotAllowedError,
    RolloutMetricsUnavailableError,
)
from oran_adapt.datastore.access import DataAccess
from oran_adapt.plugins import resolve
from oran_adapt.ports import ArmWindow

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _ident(name: str) -> str:
    return re.sub(r"\W", "_", name.rsplit(".", 1)[0])


# ---- sql: the dataset conformance suite --------------------------------------------------------
def _sql_adapter(tmp_path: Path) -> tuple[SqlDataset, dataset_conformance.Context]:
    url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    engine = create_engine(url)

    def put(name: str, data: bytes) -> str:
        if name.endswith(".jsonl"):
            records = [json.loads(line) for line in data.decode().splitlines() if line]
            header, rows = list(records[0]), [[str(r[k]) for k in records[0]] for r in records]
        else:
            lines = data.decode().splitlines()
            header, rows = lines[0].split(","), [line.split(",") for line in lines[1:]]
        table = _ident(name)
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
            conn.execute(text(f'CREATE TABLE "{table}" ('
                              + ", ".join(f'"{h}" TEXT' for h in header) + ")"))
            conn.execute(text(f'INSERT INTO "{table}" VALUES ('
                              + ", ".join(f":c{i}" for i in range(len(header))) + ")"),
                         [{f"c{i}": v for i, v in enumerate(row)} for row in rows])
        return f"sql://test/{table}.csv"

    port = SqlDataset("sql", {"test": url}, ["test.conformance_*"], time_column="ts",
                      max_bytes=1 << 20, statement_timeout_s=30)
    return port, dataset_conformance.Context(
        put=put, missing=lambda name: f"sql://test/{_ident(name)}.csv",
        outside="sql://test/secret_table")


def test_sql_dataset_conformance(tmp_path) -> None:
    port, ctx = _sql_adapter(tmp_path)
    ran = dataset_conformance.run(port, ctx)  # type: ignore[arg-type]
    assert "rows_stable" in ran


@pytest.mark.parametrize("uri", [
    "sql://test/conformance_x;drop table run",          # not one identifier
    "sql://test/conformance_x?columns=a;drop",           # not a column name
    "sql://test/conformance_x?a%20b=1",                  # not a column name
    "sql://user:pw@test/conformance_x",                  # credentials in the URI
    "sql://other/conformance_x",                         # unknown source
    "sql://test/conformance_x#frag",
    "sql://test/conformance_x.parquet",                  # only CSV is produced
])
def test_sql_refuses_without_io(tmp_path, uri) -> None:
    port, _ = _sql_adapter(tmp_path)
    with pytest.raises(DataSourceNotAllowedError):
        port.check(uri)


def test_sql_filters_bind_values_and_reject_unknown_columns(tmp_path) -> None:
    port, ctx = _sql_adapter(tmp_path)
    uri = ctx.put("conformance-f.csv", b"ts,cell,v\n2026-01-01T00:00:00,c1,1\n"
                                       b"2026-01-02T00:00:00,c2,2\n2026-01-03T00:00:00,c1,3\n")
    with port.open(uri.replace(".csv", ".csv?cell=c1&columns=ts,v")) as fh:
        assert fh.read() == b"ts,v\n2026-01-01T00:00:00,1\n2026-01-03T00:00:00,3\n"
    with port.open(uri + "?cell=c1&cell=c2&from=2026-01-02T00:00:00") as fh:
        assert fh.read().count(b"\n") == 3  # header + two rows
    with port.open(uri + "?cell=x' OR '1'='1") as fh:  # a value, never SQL
        assert fh.read() == b"ts,cell,v\n"
    with pytest.raises(DataSourceNotAllowedError, match="no column"):
        port.stat(uri + "?nope=1")


# ---- rankpi: dataset rules from the schema reference -------------------------------------------
@pytest.fixture
def rankpi_db(tmp_path) -> str:
    url = f"sqlite:///{(tmp_path / 'rankpi.db').as_posix()}"
    with create_engine(url).begin() as conn:
        conn.execute(text("CREATE TABLE run (run_id INTEGER PRIMARY KEY, label TEXT, "
                          "started_at TIMESTAMP, ended_at TIMESTAMP)"))
        conn.execute(text("CREATE TABLE v_run_summary (run_id INTEGER, dropped BIGINT)"))
        conn.execute(text("CREATE TABLE v_slice_kpi (ts TIMESTAMP, run_id INTEGER, "
                          "sst SMALLINT, sd INTEGER, dl_thp_kbps NUMERIC, prb_share NUMERIC)"))
        conn.execute(text("CREATE TABLE e2sm_mac_ue (ts TIMESTAMP, run_id INTEGER, "
                          "rnti INTEGER, wb_cqi INTEGER, dl_aggr_tbs BIGINT, slot INTEGER)"))
        conn.execute(text("INSERT INTO run VALUES (5, 'closed', :s, :e), (6, 'open', :s, NULL), "
                          "(7, 'lossy', :s, :e)"), {"s": T0, "e": T0 + timedelta(hours=1)})
        conn.execute(text("INSERT INTO v_run_summary VALUES (5, 0), (6, 0), (7, 12)"))
        conn.execute(text("INSERT INTO v_slice_kpi VALUES (:ts, :run, 1, :sd, :thp, 0.5)"),
                     [{"ts": T0 + timedelta(seconds=i), "run": run, "sd": sd, "thp": 100 + i}
                      for run in (5, 6, 7) for i in range(6) for sd in (1, 2)])
        conn.execute(text("INSERT INTO e2sm_mac_ue VALUES (:ts, 5, 17, 12, :tbs, 0)"),
                     [{"ts": T0 + timedelta(seconds=i), "tbs": 1000 * i} for i in range(4)])
    return url


def _rankpi(url: str, **overrides) -> SqlDataset:
    settings = Settings(_env_file=None, dataset_rankpi_url=url,
                        dataset_rankpi_relations=["v_*", "e2sm_mac_ue"], **overrides)
    return resolve("dataset", "rankpi", config_key="dataset_backends").factory(settings)


def test_rankpi_reads_a_closed_run_and_registers_it(rankpi_db) -> None:
    port = _rankpi(rankpi_db)
    uri = "rankpi:///v_slice_kpi?run_id=5&columns=ts,sst,sd,dl_thp_kbps"
    with port.open(uri) as fh:
        frame = pd.read_csv(fh)
    assert len(frame) == 12 and list(frame.columns) == ["ts", "sst", "sd", "dl_thp_kbps"]
    assert frame["ts"].is_monotonic_increasing
    scan = DataAccess([port]).scan(uri, fmt="csv", timestamp_column="ts")
    assert scan.row_count == 12 and scan.fingerprint == port.stat(uri).fingerprint


def test_rankpi_fingerprint_follows_the_rows(rankpi_db) -> None:
    port = _rankpi(rankpi_db)
    uri = "rankpi:///v_slice_kpi?run_id=5"
    before = port.stat(uri).fingerprint
    assert port.stat(uri).fingerprint == before
    with create_engine(rankpi_db).begin() as conn:
        conn.execute(text("UPDATE v_slice_kpi SET prb_share = 0.7 WHERE run_id = 5 AND sd = 2"))
    assert port.stat(uri).fingerprint != before


@pytest.mark.parametrize(("uri", "error", "match"), [
    ("rankpi:///v_slice_kpi", DataSourceNotAllowedError, "unbounded"),
    ("rankpi:///v_slice_kpi?run_id=6", DataSourceNotAllowedError, "still open"),
    ("rankpi:///v_slice_kpi?run_id=7", DataSourceNotAllowedError, "dropped 12 rows"),
    ("rankpi:///v_slice_kpi?run_id=5&run_id=7", DataSourceNotAllowedError, "dropped"),
    ("rankpi:///v_slice_kpi?run_id=99", DatasetNotFoundError, "run 99"),
    ("rankpi:///v_slice_kpi?run_id=five", DataSourceNotAllowedError, "integer"),
    ("rankpi:///e2sm_mac_ue?run_id=5", DataSourceNotAllowedError, "cumulative"),
    ("rankpi:///e2sm_mac_ue?run_id=5&columns=ts,dl_aggr_tbs", DataSourceNotAllowedError,
     "cumulative"),
    ("rankpi:///ue?run_id=5", DataSourceNotAllowedError, "allow-list"),
    ("rankpi:///v_missing?run_id=5", DatasetNotFoundError, "does not exist"),
])
def test_rankpi_refusals(rankpi_db, uri, error, match) -> None:
    port = _rankpi(rankpi_db)
    with pytest.raises(error, match=match):
        port.stat(uri)


def test_rankpi_rules_can_be_relaxed(rankpi_db) -> None:
    port = _rankpi(rankpi_db, dataset_rankpi_allow_cumulative=True,
                   dataset_rankpi_require_closed_run=False, dataset_rankpi_max_dropped=None)
    with port.open("rankpi:///e2sm_mac_ue?run_id=5&columns=ts,wb_cqi,dl_aggr_tbs") as fh:
        assert len(pd.read_csv(fh)) == 4
    assert port.stat("rankpi:///v_slice_kpi?run_id=6&run_id=7").size_bytes


def test_rankpi_bounded_by_time_and_safe_columns(rankpi_db) -> None:
    port = _rankpi(rankpi_db)
    uri = ("rankpi:///e2sm_mac_ue?columns=ts,rnti,wb_cqi"
           f"&to={(T0 + timedelta(seconds=1)).isoformat()}")
    with port.open(uri) as fh:
        assert len(pd.read_csv(fh)) == 2


def test_rankpi_lists_cover_the_documented_fields() -> None:
    assert {"dl_aggr_tbs", "tx_sdu_byte", "dl_pdcp_vol_kb", "dl_prb_tot"} <= RANKPI_CUMULATIVE
    assert {"slot", "dl_mcs2", "ul_aggr_bytes_sdus"} <= RANKPI_DEAD
    assert "wb_cqi" not in RANKPI_CUMULATIVE | RANKPI_DEAD


def test_rankpi_needs_its_url() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ConfigurationError, match="DATASET_RANKPI_URL"):
        resolve("dataset", "rankpi", config_key="dataset_backends").factory(settings)
    with pytest.raises(ConfigurationError, match="DATASET_SQL_SOURCES"):
        resolve("dataset", "sql", config_key="dataset_backends").factory(settings)


def test_sql_source_names_are_checked() -> None:
    settings = Settings(_env_file=None, dataset_sql_sources='{"bad name": "sqlite://"}',
                        dataset_sql_relations=["x.y"])
    with pytest.raises(ConfigurationError, match="DATASET_SQL_SOURCES"):
        resolve("dataset", "sql", config_key="dataset_backends").factory(settings)


def test_the_adapter_survives_pickling(rankpi_db) -> None:
    port = _rankpi(rankpi_db)
    port.stat("rankpi:///v_slice_kpi?run_id=5")
    copy = pickle.loads(pickle.dumps(port))
    assert copy.stat("rankpi:///v_slice_kpi?run_id=5") == port.stat(
        "rankpi:///v_slice_kpi?run_id=5")
    assert isinstance(RunGuard(True, 0), RunGuard)


# ---- rankpi: rollout metrics ------------------------------------------------------------------
_LIFECYCLE = (
    "CREATE TABLE ran_model (model_id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT)",
    ("CREATE TABLE ran_model_version (version_id INTEGER PRIMARY KEY, model_id INTEGER, "
     "version TEXT, status TEXT, UNIQUE (model_id, version))"),
    ("CREATE TABLE model_deployment (deployment_id INTEGER PRIMARY KEY, version_id INTEGER, "
     "run_id INTEGER, cell_id INTEGER, mode TEXT, started_at TIMESTAMP, ended_at TIMESTAMP)"),
    ("CREATE TABLE model_decision (decision_id INTEGER PRIMARY KEY, deployment_id INTEGER, "
     "step INTEGER, ts TIMESTAMP, score NUMERIC, inference_ms NUMERIC)"),
    ("CREATE TABLE score_term (decision_id INTEGER, name TEXT, raw_value NUMERIC, "
     "weight NUMERIC, contribution NUMERIC, PRIMARY KEY (decision_id, name))"),
    ("CREATE TABLE control_action (action_id INTEGER PRIMARY KEY, decision_id INTEGER, "
     "clipped BOOLEAN)"),
    ("CREATE TABLE control_delivery (delivery_id INTEGER PRIMARY KEY, decision_id INTEGER, "
     "outcome TEXT)"),
    ("CREATE TABLE model_metric (metric_id INTEGER PRIMARY KEY, deployment_id INTEGER, "
     "name TEXT, step INTEGER, ts TIMESTAMP, value NUMERIC, UNIQUE (deployment_id, name, step))"),
)


@pytest.fixture
def lifecycle_db(tmp_path) -> str:
    url = f"sqlite:///{(tmp_path / 'lifecycle.db').as_posix()}"
    with create_engine(url).begin() as conn:
        for ddl in _LIFECYCLE:
            conn.execute(text(ddl))
    return url


def _deployment(conn, model: str, version: str, started: datetime) -> int:
    conn.execute(text("INSERT OR IGNORE INTO ran_model (name, kind) VALUES (:m, 'drl')"),
                 {"m": model})
    model_id = conn.execute(text("SELECT model_id FROM ran_model WHERE name = :m"),
                            {"m": model}).scalar_one()
    conn.execute(text("INSERT OR IGNORE INTO ran_model_version (model_id, version, status) "
                      "VALUES (:id, :v, 'candidate')"), {"id": model_id, "v": version})
    version_id = conn.execute(text("SELECT version_id FROM ran_model_version WHERE model_id = "
                                   ":id AND version = :v"),
                              {"id": model_id, "v": version}).scalar_one()
    found = conn.execute(text("SELECT deployment_id FROM model_deployment WHERE version_id = "
                              ":v"), {"v": version_id}).scalar()
    if found is not None:
        return int(found)
    conn.execute(text("INSERT INTO model_deployment (version_id, mode, started_at) "
                      "VALUES (:v, 'shadow', :s)"), {"v": version_id, "s": started})
    return int(conn.execute(text("SELECT max(deployment_id) FROM model_deployment")).scalar())


def test_rankpi_rollout_metrics_conformance(lifecycle_db) -> None:
    port = RankpiRolloutMetrics(lifecycle_db)
    steps = iter(range(1, 10_000))
    engine = create_engine(lifecycle_db)

    def seed(window: ArmWindow, at: datetime, requests: int, metrics: dict[str, float]) -> None:
        with engine.begin() as conn:
            dep = _deployment(conn, window.model, window.version, window.start - timedelta(
                hours=2))
            for name, value in metrics.items():
                conn.execute(text("INSERT INTO model_metric (deployment_id, name, step, ts, "
                                  "value) VALUES (:d, :n, :s, :ts, :v)"),
                             {"d": dep, "n": name, "s": next(steps), "ts": at, "v": value})

    def break_source() -> None:
        port.url = "sqlite:////nonexistent-dir/rankpi.db"
        port._engine = None

    # The adapter reads its own database, never the framework's session.
    ran = rollout_conformance.run(port, rollout_conformance.Context(
        session=None, seed=seed, break_source=break_source))
    assert "unreachable_source" in ran


def test_rankpi_rollout_metrics_read_the_decision_facts(lifecycle_db) -> None:
    engine = create_engine(lifecycle_db)
    with engine.begin() as conn:
        dep = _deployment(conn, "slice-ppo", "3", T0 - timedelta(hours=1))
        other = _deployment(conn, "slice-ppo", "2", T0 - timedelta(hours=1))
        for i, (score, clipped, outcome) in enumerate(
            [(0.5, False, "ack"), (0.7, True, "timeout"), (0.2, False, "suppressed")]
        ):
            conn.execute(text("INSERT INTO model_decision (decision_id, deployment_id, step, "
                              "ts, score, inference_ms) VALUES (:id, :d, :s, :ts, :sc, 4)"),
                         {"id": i + 1, "d": dep, "s": i, "ts": T0 + timedelta(seconds=i),
                          "sc": score})
            conn.execute(text("INSERT INTO score_term VALUES (:id, 'throughput', 1, 0.1, :c), "
                              "(:id, 'starvation', 1, 1, -0.3)"), {"id": i + 1, "c": score})
            conn.execute(text("INSERT INTO control_action (decision_id, clipped) "
                              "VALUES (:id, :c)"), {"id": i + 1, "c": clipped})
            conn.execute(text("INSERT INTO control_delivery (decision_id, outcome) "
                              "VALUES (:id, :o)"), {"id": i + 1, "o": outcome})
        conn.execute(text("INSERT INTO model_decision (deployment_id, step, ts, score) "
                          "VALUES (:d, 0, :ts, 9.0)"), {"d": other, "ts": T0})
    port = RankpiRolloutMetrics(lifecycle_db, model_names={"slice-model": "slice-ppo"})
    window = ArmWindow(rollout_id="r1", model="slice-model", arm="candidate", version="3",
                       start=T0, end=T0 + timedelta(minutes=1))
    stats = port.observe(None, window)  # type: ignore[arg-type]
    assert stats.count == 3
    assert stats.samples["score"] == [0.5, 0.7, 0.2]
    assert stats.samples["score_term.starvation"] == [-0.3, -0.3, -0.3]
    assert stats.samples["clipped"] == [0.0, 1.0, 0.0]
    assert stats.samples["delivery_failed"] == [0.0, 1.0]
    assert stats.samples["inference_ms"] == [4.0, 4.0, 4.0]


def test_rankpi_rollout_metrics_need_a_url() -> None:
    with pytest.raises(ConfigurationError, match="ROLLOUT_RANKPI_URL"):
        resolve("rollout_metrics", "rankpi", config_key="rollout_metrics_backend").factory(
            Settings(_env_file=None))
    with pytest.raises(RolloutMetricsUnavailableError):
        RankpiRolloutMetrics("sqlite:////nonexistent-dir/x.db").ping()


def test_csv_round_trip_keeps_types(rankpi_db) -> None:
    port = _rankpi(rankpi_db)
    with port.open("rankpi:///v_slice_kpi?run_id=5&sd=2") as fh:
        frame = pd.read_csv(io.BytesIO(fh.read()))
    assert set(frame["sd"]) == {2} and frame["dl_thp_kbps"].dtype.kind in "if"


def test_the_rankpi_example_and_policy_lint() -> None:
    from oran_adapt.core.config import lint_config_file
    from oran_adapt.core.config_sources import read_config_file
    from oran_adapt.core.policies import DeliveryPolicy, load_policy_file

    root = Path(__file__).resolve().parents[2]
    example = root / "config" / "examples" / "rankpi.toml"
    assert lint_config_file(str(example)) == []
    values = read_config_file(Settings, str(example))
    assert values["dataset_backends"] == "rankpi" and values["delivery_strategy"] == "shadow"
    policy = load_policy_file(str(root / values["delivery_policy_file"]), DeliveryPolicy,
                              "delivery_policy_file")
    assert {"score", "clipped", "delivery_failed"} <= {rule.metric for rule in policy.health}
