"""Rollout metrics adapter ``rankpi`` (docs/integration/rankpi.md): a rollout arm's online
health read from the ML lifecycle store of the OAI RAN KPI database (schema_model.sql).

An arm (model, version, window) maps to every ``model_deployment`` of that ``ran_model.name``
(ROLLOUT_RANKPI_MODEL_NAMES maps the framework's model name when they differ) and
``ran_model_version.version`` that overlaps the window. Within the window it reads:

- ``score`` and ``inference_ms``: one sample per ``model_decision``. The decision count is the
  arm's request count;
- ``score_term.<name>``: each ``score_term.contribution``, so a health rule can watch one reward
  component (a starvation or fairness penalty) instead of the total, whose unbounded throughput
  term can hide a starved slice;
- ``clipped``: 1.0 or 0.0 per ``control_action``, so its mean is the clipped fraction;
- ``delivery_failed``: 1.0 per ``control_delivery`` with outcome nack, timeout or error, 0.0
  for sent or ack (suppressed deliveries are not counted);
- every ``model_metric`` row, under its own name.

The adapter only reads, in a read-only transaction on PostgreSQL. The window's arm label is not
used: the stable and candidate arms are told apart by their versions.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, create_engine, exc, text
from sqlalchemy.engine import Connection, Engine

from oran_adapt.core.errors import ConfigurationError, RolloutMetricsUnavailableError
from oran_adapt.ports import AdapterSpec, ArmStats, ArmWindow, Capability

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from oran_adapt.core.config import Settings

_DEPLOYMENTS = text(
    "SELECT d.deployment_id FROM model_deployment d"
    " JOIN ran_model_version v ON v.version_id = d.version_id"
    " JOIN ran_model m ON m.model_id = v.model_id"
    " WHERE m.name = :model AND v.version = :version"
    " AND d.started_at <= :end AND (d.ended_at IS NULL OR d.ended_at >= :start)"
)
_IN_WINDOW = "deployment_id IN :ids AND ts >= :start AND ts <= :end"
_DECISIONS = text(f"SELECT score, inference_ms FROM model_decision WHERE {_IN_WINDOW}")
_DECISION_IDS = f"SELECT decision_id FROM model_decision WHERE {_IN_WINDOW}"
_TERMS = text(f"SELECT name, contribution FROM score_term WHERE decision_id IN ({_DECISION_IDS})")
_CLIPPED = text(f"SELECT clipped FROM control_action WHERE decision_id IN ({_DECISION_IDS})")
_DELIVERIES = text(
    f"SELECT outcome FROM control_delivery WHERE decision_id IN ({_DECISION_IDS})"
)
_METRICS = text(f"SELECT name, value FROM model_metric WHERE {_IN_WINDOW}")
_FAILED = frozenset({"nack", "timeout", "error"})
_DELIVERED = frozenset({"sent", "ack"})


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class RankpiRolloutMetrics:
    def __init__(self, url: str, *, model_names: dict[str, str] | None = None,
                 timeout_s: float = 30.0) -> None:
        self.url = url
        self.model_names = dict(model_names or {})
        self.timeout_s = timeout_s
        self._engine: Engine | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> RankpiRolloutMetrics:
        if settings.rollout_rankpi_url is None:
            raise ConfigurationError(
                "ROLLOUT_METRICS_BACKEND=rankpi needs ROLLOUT_RANKPI_URL",
                key="rollout_rankpi_url",
            )
        return cls(settings.rollout_rankpi_url.get_secret_value(),
                   model_names=settings.rollout_rankpi_model_names,
                   timeout_s=settings.rollout_rankpi_timeout_s)

    # The controller runs in worker processes; a copy opens its own connections.
    def __getstate__(self) -> dict[str, Any]:
        return {**self.__dict__, "_engine": None}

    def _connect(self) -> Connection:
        if self._engine is None:
            self._engine = create_engine(self.url, pool_pre_ping=True)
        conn = self._engine.connect()
        if conn.dialect.name == "postgresql":
            conn = conn.execution_options(postgresql_readonly=True)
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"),
                         {"ms": str(int(self.timeout_s * 1000))})
        return conn

    def ping(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(text("SELECT 1"))
        except exc.DBAPIError as error:
            raise RolloutMetricsUnavailableError(
                "the rankpi database could not be reached", cause=type(error.orig).__name__
            ) from error

    def observe(self, session: Session, window: ArmWindow) -> ArmStats:
        params: dict[str, Any] = {
            "model": self.model_names.get(window.model, window.model),
            "version": window.version, "start": window.start, "end": window.end,
        }
        samples: dict[str, list[float]] = {}

        def add(name: str, value: Any) -> None:
            number = _float(value)
            if number is not None:
                samples.setdefault(name, []).append(number)

        try:
            with self._connect() as conn:
                ids = [row[0] for row in conn.execute(_DEPLOYMENTS, params)]
                if not ids:
                    return ArmStats(samples={}, count=0)
                params["ids"] = ids

                def rows(query: Any) -> Any:
                    return conn.execute(
                        query.bindparams(bindparam("ids", expanding=True)), params)

                decisions = 0
                for score, inference_ms in rows(_DECISIONS):
                    decisions += 1
                    add("score", score)
                    add("inference_ms", inference_ms)
                for name, contribution in rows(_TERMS):
                    add(f"score_term.{name}", contribution)
                for (clipped,) in rows(_CLIPPED):
                    if clipped is not None:
                        add("clipped", 1.0 if clipped else 0.0)
                for (outcome,) in rows(_DELIVERIES):
                    if outcome in _FAILED:
                        add("delivery_failed", 1.0)
                    elif outcome in _DELIVERED:
                        add("delivery_failed", 0.0)
                metric_rows = 0
                for name, value in rows(_METRICS):
                    metric_rows += 1
                    add(str(name), value)
        except exc.DBAPIError as error:
            raise RolloutMetricsUnavailableError(
                "the rankpi database could not be read", cause=type(error.orig).__name__,
                model=window.model, version=window.version,
            ) from error
        return ArmStats(samples=samples, count=decisions or metric_rows)


def _rankpi(settings: Settings) -> RankpiRolloutMetrics:
    return RankpiRolloutMetrics.from_settings(settings)


RANKPI = AdapterSpec(
    capability=Capability(
        port="rollout_metrics",
        adapter="rankpi",
        description="decision scores, reward terms, clipping, delivery failures and "
                    "model_metric rows from the rankpi ML lifecycle store",
        features=frozenset({"pull"}),
        config_keys=("rollout_rankpi_url", "rollout_rankpi_model_names",
                     "rollout_rankpi_timeout_s"),
        required_keys=("rollout_rankpi_url",),
    ),
    factory=_rankpi,
)

__all__ = ["RANKPI", "RankpiRolloutMetrics"]
