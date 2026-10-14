"""Dataset adapters (port ``dataset``) that read a data version by **database query** instead of
by file: ``sql`` (any SQLAlchemy database) and ``rankpi`` (the OAI RAN KPI + ML database,
PostgreSQL/TimescaleDB, docs/integration/rankpi.md).

A reference names one relation (table or view) and its filters, never SQL text::

    sql://<source>/<relation>[.csv]?<column>=<value>&from=<ts>&to=<ts>&columns=a,b
    rankpi:///<relation>[.csv]?run_id=5&columns=ts,sst,sd,dl_thp_kbps

``<source>`` is a name in DATASET_SQL_SOURCES (``rankpi`` has the one source DATASET_RANKPI_URL,
so its URIs have an empty host). The relation must match the adapter's allow-list
(DATASET_SQL_RELATIONS as ``source.relation`` patterns, DATASET_RANKPI_RELATIONS as relation
patterns; ``fnmatch`` wildcards). Every other query parameter is an equality filter on a column
(repeat it for IN); ``from``/``to`` bound the time column (``time_column=`` or the adapter's
default), inclusive. Identifiers are checked against the relation's reflected columns and
quoted by SQLAlchemy, values are bound parameters, and on PostgreSQL the query runs in a
read-only transaction under DATASET_SQL_STATEMENT_TIMEOUT_S.

The result is streamed (server-side cursor) as CSV, ordered by the time column and then every
other selected column so the same rows always give the same bytes. ``stat`` fingerprints those
bytes (SHA-256), which is how a registered version notices that the rows behind its query
changed (``identify_ues.py`` filling ``ue_session.ue_id`` later changes identity views, for
instance), so it reads the result once more than ``open`` does.

``rankpi`` adds the rules from the schema reference:

- a query must be bounded by ``run_id`` or ``to``, so a live, still-growing relation never
  backs a version;
- a ``run_id`` must name a closed run (``run.ended_at`` set; DATASET_RANKPI_REQUIRE_CLOSED_RUN)
  whose collector dropped at most DATASET_RANKPI_MAX_DROPPED rows (``v_run_summary.dropped``);
- cumulative counters (``dl_aggr_tbs``, ``tx_pdu_byte``, ...) and known dead fields (``slot``,
  ``dl_mcs2``, ``*_aggr_bytes_sdus``) are refused as columns. A cumulative counter grows for the
  whole run, so every drift test flags it; read the rate views (``v_ue_rate``, ``v_slice_kpi``)
  instead, or set DATASET_RANKPI_ALLOW_CUMULATIVE.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import bindparam, column, create_engine, exc, inspect, select, table, text
from sqlalchemy.engine import Connection, Engine

from oran_adapt.adapters.datasets import not_allowed, required, spool
from oran_adapt.core.errors import (
    ConfigurationError,
    DatasetNotFoundError,
    DataSourceNotAllowedError,
    DataSourceUnavailableError,
    DataTooLargeError,
)
from oran_adapt.ports import AdapterSpec, Capability, SourceStat

if TYPE_CHECKING:
    from pydantic import SecretStr

    from oran_adapt.core.config import Settings

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"columns", "from", "to", "time_column"})
_ROWS_PER_CHUNK = 1000

# rankpi schema reference (schema.sql, migrations/002_kpm.sql): columns that are cumulative
# counters ("never sum across rows, use MAX-MIN for a true delta") and fields documented as dead.
RANKPI_CUMULATIVE = frozenset({
    "dl_aggr_tbs", "ul_aggr_tbs", "dl_aggr_sdus", "ul_aggr_sdus", "dl_aggr_prb", "ul_aggr_prb",
    "dl_aggr_retx_prb", "ul_aggr_retx_prb",
    "dl_harq_r1", "dl_harq_r2", "dl_harq_r3", "dl_harq_r4", "dl_harq_fail",
    "ul_harq_r1", "ul_harq_r2", "ul_harq_r3", "ul_harq_r4", "ul_harq_fail",
    "tx_pdu_pkt", "tx_pdu_byte", "rx_pdu_pkt", "rx_pdu_byte",
    "tx_sdu_pkt", "tx_sdu_byte", "rx_sdu_pkt", "rx_sdu_byte",
    "dl_pdcp_vol_kb", "ul_pdcp_vol_kb", "dl_prb_tot", "ul_prb_tot",
})
RANKPI_DEAD = frozenset({"slot", "dl_mcs2", "ul_mcs2", "dl_aggr_bytes_sdus",
                         "ul_aggr_bytes_sdus"})


@dataclass(frozen=True)
class Query:
    """One parsed reference: where to read and what."""

    source: str
    relation: str
    columns: tuple[str, ...]  # empty: every column of the relation
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    time_column: str
    start: str | None
    end: str | None


@dataclass(frozen=True)
class RunGuard:
    """rankpi run checks: ``run.ended_at`` must be set, ``v_run_summary.dropped`` at most
    ``max_dropped`` (None: not checked)."""

    require_closed: bool
    max_dropped: int | None


def _coerce(value: str, sql_type: Any) -> Any:
    """A query-string value as the Python type of the column it is compared with."""
    try:
        python_type = sql_type.python_type
    except (NotImplementedError, AttributeError):
        return value
    try:
        if python_type is bool:
            return value.strip().lower() in ("1", "true", "t", "yes")
        if python_type in (int, float, Decimal):
            return python_type(value)
        if python_type is datetime:
            # An unencoded "+00:00" offset arrives as " 00:00" from the query string.
            value = re.sub(r" (\d{2}:?\d{2})$", r"+\1", value.replace("Z", "+00:00"))
            return datetime.fromisoformat(value)
        if python_type is date:
            return date.fromisoformat(value)
    except ValueError:
        raise DataSourceNotAllowedError(
            f"filter value {value!r} is not a valid {python_type.__name__}", value=value
        ) from None
    return value


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    return str(value)


class SqlDataset:
    """Reads one allow-listed relation per reference, as CSV. ``sources`` maps a source name
    (the URI host; "" for a single-source adapter) to a SQLAlchemy URL; ``relations`` are the
    allowed ``source.relation`` (or bare ``relation`` for source "") patterns."""

    def __init__(
        self,
        scheme: str,
        sources: Mapping[str, str],
        relations: Iterable[str],
        *,
        time_column: str,
        max_bytes: int,
        statement_timeout_s: float,
        spool_dir: str | None = None,
        require_bound: Sequence[str] | None = None,
        refused_columns: Iterable[str] = (),
        run_guard: RunGuard | None = None,
    ) -> None:
        self.schemes = frozenset({scheme})
        self._sources = dict(sources)
        self.relations = list(relations)
        if not self.relations:
            raise ConfigurationError(f"the {scheme} dataset adapter has an empty relation "
                                     "allow-list", key="relations")
        self.time_column = time_column
        self.max_bytes = max_bytes
        self.statement_timeout_s = statement_timeout_s
        self.spool_dir = spool_dir
        self.require_bound = tuple(require_bound) if require_bound is not None else None
        self.refused_columns = frozenset(refused_columns)
        self.run_guard = run_guard
        self._engines: dict[str, Engine] = {}

    # Engines hold sockets; a copy (pickled into a worker) opens its own.
    def __getstate__(self) -> dict[str, Any]:
        return {**self.__dict__, "_engines": {}}

    # ------------------------------------------------------------------ parsing (no I/O)
    def _parse(self, uri: str) -> Query:
        parts = urlsplit(uri)
        if parts.scheme.lower() not in self.schemes:
            raise not_allowed(uri, f"not a {'/'.join(sorted(self.schemes))} URI")
        if parts.username or parts.password or parts.port:
            raise not_allowed(uri, "credentials and ports are configured, not put in the URI")
        if parts.fragment:
            raise not_allowed(uri, "database references take no fragment")
        source = parts.netloc
        if source not in self._sources:
            raise not_allowed(uri, "unknown database source", source=source,
                              known=sorted(self._sources))
        relation = parts.path.lstrip("/")
        if relation.lower().endswith(".csv"):
            relation = relation[:-4]
        if not _IDENT.match(relation):
            raise not_allowed(uri, "the path must name one relation (letters, digits, _)")
        key = f"{source}.{relation}" if source else relation
        if not any(fnmatchcase(key, pattern) for pattern in self.relations):
            raise not_allowed(uri, "relation is outside the adapter's relation allow-list",
                              relation=key)
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True,
                              strict_parsing=bool(parts.query))
        except ValueError:
            raise not_allowed(uri, "the query string is malformed") from None
        columns: tuple[str, ...] = ()
        filters: dict[str, list[str]] = {}
        params: dict[str, str] = {}
        for name, value in pairs:
            if name == "columns":
                columns = tuple(c.strip() for c in value.split(",") if c.strip())
            elif name in _RESERVED:
                params[name] = value
            else:
                filters.setdefault(name, []).append(value)
        time_column = params.get("time_column") or self.time_column
        for ident in (*columns, *filters, time_column):
            if not _IDENT.match(ident):
                raise not_allowed(uri, f"{ident!r} is not a column name")
        refused = sorted(self.refused_columns.intersection(columns))
        if refused:
            raise self._refused(uri, refused)
        query = Query(source, relation, columns,
                      tuple((k, tuple(v)) for k, v in sorted(filters.items())),
                      time_column, params.get("from"), params.get("to"))
        if self.require_bound is not None and query.end is None and not any(
            k in self.require_bound for k, _ in query.filters
        ):
            raise not_allowed(uri, "the query is unbounded: filter on "
                              f"{' or '.join(self.require_bound) or 'a column'} or give 'to', "
                              "so the rows behind a data version stop changing")
        return query

    def _refused(self, uri: str, names: Sequence[str]) -> DataSourceNotAllowedError:
        return not_allowed(
            uri, "cumulative counters or dead fields are refused as columns (drift tests flag "
            "a counter that grows all run); read a rate view instead", columns=list(names))

    def check(self, uri: str) -> None:
        self._parse(uri)

    # ------------------------------------------------------------------ database access
    def _engine(self, source: str) -> Engine:
        engine = self._engines.get(source)
        if engine is None:
            engine = create_engine(self._sources[source], pool_pre_ping=True)
            self._engines[source] = engine
        return engine

    def _begin(self, conn: Connection) -> Connection:
        if conn.dialect.name == "postgresql":
            conn = conn.execution_options(postgresql_readonly=True)
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                         {"ms": str(int(self.statement_timeout_s * 1000))})
        return conn

    def _columns(self, conn: Connection, uri: str, relation: str) -> dict[str, Any]:
        try:
            reflected = inspect(conn).get_columns(relation)
        except exc.NoSuchTableError:
            reflected = []
        if not reflected:
            raise DatasetNotFoundError("the referenced relation does not exist", uri=uri,
                                       relation=relation)
        return {c["name"]: c["type"] for c in reflected}

    def _guard_runs(self, conn: Connection, uri: str, runs: Sequence[str]) -> None:
        guard = self.run_guard
        if guard is None or not runs:
            return
        ids = [int(r) for r in runs]
        if guard.require_closed:
            ended = dict(conn.execute(
                text("SELECT run_id, ended_at FROM run WHERE run_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all())
            for run_id in ids:
                if run_id not in ended:
                    raise DatasetNotFoundError(f"run {run_id} does not exist", uri=uri)
                if ended[run_id] is None:
                    raise not_allowed(uri, f"run {run_id} is still open (run.ended_at is "
                                      "NULL); register it once the run has ended",
                                      run_id=run_id)
        if guard.max_dropped is not None:
            dropped = dict(conn.execute(
                text("SELECT run_id, dropped FROM v_run_summary WHERE run_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all())
            for run_id in ids:
                lost = dropped.get(run_id)
                if lost is None and run_id not in dropped:
                    raise not_allowed(uri, f"run {run_id} has no v_run_summary row, so its "
                                      "collector data loss cannot be checked", run_id=run_id)
                if lost is not None and int(lost) > guard.max_dropped:
                    raise not_allowed(uri, f"the collector dropped {lost} rows in run {run_id} "
                                      "(v_run_summary.dropped); its data is incomplete",
                                      run_id=run_id, dropped=int(lost),
                                      limit=guard.max_dropped)

    def _chunks(self, uri: str) -> Iterator[bytes]:
        """The reference's rows as CSV bytes, a few hundred rows per chunk."""
        query = self._parse(uri)
        try:
            with self._engine(query.source).connect() as raw:
                conn = self._begin(raw)
                types = self._columns(conn, uri, query.relation)
                runs = dict(query.filters).get("run_id", ())
                if self.run_guard is not None and runs and "run_id" in types:
                    try:
                        self._guard_runs(conn, uri, runs)
                    except ValueError:
                        raise not_allowed(uri, "run_id must be an integer") from None
                selected = list(query.columns or types)
                for name in (*selected, *(k for k, _ in query.filters)):
                    if name not in types:
                        raise not_allowed(uri, f"the relation has no column {name!r}",
                                          relation=query.relation)
                refused = sorted(self.refused_columns.intersection(selected))
                if refused:
                    raise self._refused(uri, refused)
                if (query.start or query.end) and query.time_column not in types:
                    raise not_allowed(uri, f"from/to need the time column {query.time_column!r}"
                                      ", which the relation lacks (set time_column=)")
                result = conn.execution_options(stream_results=True,
                                                yield_per=_ROWS_PER_CHUNK).execute(
                    self._select(query, selected, types))
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                writer.writerow(selected)
                for i, row in enumerate(result, start=1):
                    writer.writerow([_cell(v) for v in row])
                    if i % _ROWS_PER_CHUNK == 0:
                        yield buffer.getvalue().encode("utf-8")
                        buffer.seek(0)
                        buffer.truncate()
                yield buffer.getvalue().encode("utf-8")
        except exc.DBAPIError as error:
            raise DataSourceUnavailableError(
                f"database query failed: {type(error.orig).__name__}", uri=uri
            ) from error

    def _select(self, query: Query, selected: list[str], types: Mapping[str, Any]) -> Any:
        rel = table(query.relation, *(column(n, types[n]) for n in types))
        stmt = select(*(rel.c[n] for n in selected))
        for name, values in query.filters:
            col = rel.c[name]
            coerced = [_coerce(v, types[name]) for v in values]
            stmt = stmt.where(col == coerced[0] if len(coerced) == 1 else col.in_(coerced))
        time_col = rel.c[query.time_column] if query.time_column in types else None
        if time_col is not None and query.start:
            stmt = stmt.where(time_col >= _coerce(query.start, types[query.time_column]))
        if time_col is not None and query.end:
            stmt = stmt.where(time_col <= _coerce(query.end, types[query.time_column]))
        order = ([query.time_column] if query.time_column in selected else []) + [
            n for n in selected if n != query.time_column]
        return stmt.order_by(*(rel.c[n] for n in order))

    # ------------------------------------------------------------------ DatasetPort
    def stat(self, uri: str) -> SourceStat:
        digest = hashlib.sha256()
        size = 0
        for chunk in self._chunks(uri):
            size += len(chunk)
            if size > self.max_bytes:
                raise DataTooLargeError(
                    "the query result is larger than DATASET_MAX_SOURCE_BYTES",
                    key="DATASET_MAX_SOURCE_BYTES", limit=self.max_bytes, uri=uri)
            digest.update(chunk)
        return SourceStat(uri=uri, pinned_uri=uri, fingerprint=f"sha256:{digest.hexdigest()}",
                          size_bytes=size)

    def open(self, uri: str) -> BinaryIO:
        return spool(self._chunks(uri), max_bytes=self.max_bytes, spool_dir=self.spool_dir,
                     uri=uri)


def _sources(settings: Settings) -> dict[str, str]:
    raw: SecretStr = required(settings, "dataset_sql_sources", "sql")
    try:
        sources = json.loads(raw.get_secret_value())
    except ValueError:
        raise ConfigurationError("DATASET_SQL_SOURCES is not valid JSON",
                                 key="DATASET_SQL_SOURCES") from None
    if not isinstance(sources, dict) or not sources or not all(
        isinstance(k, str) and _IDENT.match(k) and isinstance(v, str) and v
        for k, v in sources.items()
    ):
        raise ConfigurationError(
            'DATASET_SQL_SOURCES must be {"<name>": "<SQLAlchemy URL>", ...} with names of '
            "letters, digits and _", key="DATASET_SQL_SOURCES")
    return sources


def _sql(settings: Settings) -> SqlDataset:
    return SqlDataset(
        "sql",
        _sources(settings),
        required(settings, "dataset_sql_relations", "sql"),
        time_column=settings.dataset_time_column,
        max_bytes=settings.dataset_max_source_bytes,
        statement_timeout_s=settings.dataset_sql_statement_timeout_s,
        spool_dir=settings.dataset_spool_dir,
    )


def _rankpi(settings: Settings) -> SqlDataset:
    url: SecretStr = required(settings, "dataset_rankpi_url", "rankpi")
    return SqlDataset(
        "rankpi",
        {"": url.get_secret_value()},
        required(settings, "dataset_rankpi_relations", "rankpi"),
        time_column=settings.dataset_rankpi_time_column,
        max_bytes=settings.dataset_max_source_bytes,
        statement_timeout_s=settings.dataset_sql_statement_timeout_s,
        spool_dir=settings.dataset_spool_dir,
        require_bound=("run_id",),
        refused_columns=() if settings.dataset_rankpi_allow_cumulative
        else RANKPI_CUMULATIVE | RANKPI_DEAD,
        run_guard=RunGuard(settings.dataset_rankpi_require_closed_run,
                           settings.dataset_rankpi_max_dropped),
    )


SQL = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="sql",
        description="rows of an allow-listed table or view (DATASET_SQL_RELATIONS) of a "
                    "DATASET_SQL_SOURCES database, filtered by URI parameters, read as CSV",
        features=frozenset({"network", "fingerprint", "query"}),
        config_keys=("dataset_sql_sources", "dataset_sql_relations",
                     "dataset_sql_statement_timeout_s", "dataset_time_column",
                     "dataset_max_source_bytes", "dataset_spool_dir"),
        required_keys=("dataset_sql_sources", "dataset_sql_relations"),
    ),
    factory=_sql,
)

RANKPI = AdapterSpec(
    capability=Capability(
        port="dataset",
        adapter="rankpi",
        description="the OAI RAN KPI database: bounded queries of allow-listed views, closed "
                    "runs without collector data loss, no cumulative counters",
        features=frozenset({"network", "fingerprint", "query"}),
        config_keys=("dataset_rankpi_url", "dataset_rankpi_relations",
                     "dataset_rankpi_time_column", "dataset_rankpi_require_closed_run",
                     "dataset_rankpi_max_dropped", "dataset_rankpi_allow_cumulative",
                     "dataset_sql_statement_timeout_s", "dataset_max_source_bytes",
                     "dataset_spool_dir"),
        required_keys=("dataset_rankpi_url", "dataset_rankpi_relations"),
    ),
    factory=_rankpi,
)

__all__ = ["RANKPI", "RANKPI_CUMULATIVE", "RANKPI_DEAD", "SQL", "RunGuard", "SqlDataset"]
