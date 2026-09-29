"""Rollout metrics adapters (docs/adapters/rollout_metrics.md): where a rollout's online health
metrics come from.

- ``api`` (the default): observations POSTed to ``/api/v1/rollouts/{id}/observations`` by the
  serving layer or a sidecar, stored as rollout_observation rows. Needs nothing else running.
- ``prometheus``: PromQL range queries (ROLLOUT_PROMETHEUS_QUERIES), one per metric name, over
  the arm's window; every sample of every returned series is one sample of that metric. The
  requests query (ROLLOUT_PROMETHEUS_REQUESTS_QUERY), an instant query at the window's end,
  counts the arm's requests.

Query templates may use ``{model}``, ``{version}``, ``{arm}``, ``{rollout_id}`` and
``{window_s}``; only these are replaced, so PromQL's own braces need no escaping.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from oran_adapt.core.errors import ConfigurationError, RolloutMetricsUnavailableError
from oran_adapt.db.models import RolloutObservation
from oran_adapt.ports import AdapterSpec, ArmStats, ArmWindow, Capability

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.orm import Session

    from oran_adapt.core.config import Settings
    from oran_adapt.ports import RolloutMetricsPort


class ApiRolloutMetrics:
    """Reads the observations submitted through the API for the arm within the window."""

    def ping(self) -> None:
        return None

    def observe(self, session: Session, window: ArmWindow) -> ArmStats:
        rows = session.scalars(
            select(RolloutObservation).where(
                RolloutObservation.rollout_id == window.rollout_id,
                RolloutObservation.arm == window.arm,
                RolloutObservation.observed_at >= window.start,
                RolloutObservation.observed_at <= window.end,
            ).order_by(RolloutObservation.id)
        )
        samples: dict[str, list[float]] = {}
        count = 0
        for row in rows:
            count += int(row.requests)
            for name, value in (row.metrics or {}).items():
                if isinstance(value, int | float) and math.isfinite(float(value)):
                    samples.setdefault(name, []).append(float(value))
        return ArmStats(samples=samples, count=count)


def fill(template: str, window: ArmWindow) -> str:
    values = {
        "model": window.model,
        "version": window.version,
        "arm": window.arm,
        "rollout_id": window.rollout_id,
        "window_s": str(max(1, int((window.end - window.start).total_seconds()))),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


class PrometheusRolloutMetrics:
    def __init__(self, url: str, *, queries: dict[str, str], requests_query: str | None,
                 step_s: float, timeout_s: float, token: SecretStr | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.url = url.rstrip("/")
        self.queries = queries
        self.requests_query = requests_query
        self.step_s = step_s
        self.timeout_s = timeout_s
        self.token = token
        self.transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> PrometheusRolloutMetrics:
        if not settings.rollout_prometheus_url:
            raise ConfigurationError(
                "ROLLOUT_METRICS_BACKEND=prometheus needs ROLLOUT_PROMETHEUS_URL",
                key="rollout_prometheus_url",
            )
        if not settings.rollout_prometheus_queries:
            raise ConfigurationError(
                "ROLLOUT_METRICS_BACKEND=prometheus needs ROLLOUT_PROMETHEUS_QUERIES "
                "(metric name -> PromQL)",
                key="rollout_prometheus_queries",
            )
        return cls(
            settings.rollout_prometheus_url,
            queries=dict(settings.rollout_prometheus_queries),
            requests_query=settings.rollout_prometheus_requests_query,
            step_s=settings.rollout_prometheus_step_s,
            timeout_s=settings.rollout_prometheus_timeout_s,
            token=settings.rollout_prometheus_token,
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                response = client.get(f"{self.url}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise RolloutMetricsUnavailableError(
                "Prometheus could not be reached", url=self.url, cause=str(exc)
            ) from exc
        if response.status_code >= 400:
            raise RolloutMetricsUnavailableError(
                f"Prometheus answered HTTP {response.status_code}", url=self.url, path=path,
                cause=response.text[:500],
            )
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RolloutMetricsUnavailableError(
                "Prometheus answered something that is not JSON", url=self.url,
                cause=str(exc),
            ) from exc
        if body.get("status") != "success":
            raise RolloutMetricsUnavailableError(
                "Prometheus reported a failed query", url=self.url,
                cause=str(body.get("error", ""))[:500],
            )
        data: dict[str, Any] = body.get("data") or {}
        return data

    def ping(self) -> None:
        self._get("/api/v1/status/buildinfo", {})

    @staticmethod
    def _floats(pairs: list[Any]) -> list[float]:
        out = []
        for pair in pairs:
            try:
                value = float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(value):
                out.append(value)
        return out

    def observe(self, session: Session, window: ArmWindow) -> ArmStats:
        samples: dict[str, list[float]] = {}
        params = {"start": window.start.timestamp(), "end": window.end.timestamp(),
                  "step": self.step_s}
        for name, template in sorted(self.queries.items()):
            data = self._get("/api/v1/query_range", {**params, "query": fill(template, window)})
            values: list[float] = []
            for series in data.get("result") or []:
                values.extend(self._floats(series.get("values") or []))
            if values:
                samples[name] = values
        count = 0
        if self.requests_query:
            data = self._get("/api/v1/query", {"query": fill(self.requests_query, window),
                                               "time": window.end.timestamp()})
            count = int(sum(
                v for series in data.get("result") or []
                for v in self._floats([series.get("value") or []])
            ))
        else:
            count = max((len(v) for v in samples.values()), default=0)
        return ArmStats(samples=samples, count=count)


def _api(settings: Settings) -> RolloutMetricsPort:
    return ApiRolloutMetrics()


def _prometheus(settings: Settings) -> RolloutMetricsPort:
    return PrometheusRolloutMetrics.from_settings(settings)


API = AdapterSpec(
    capability=Capability(
        port="rollout_metrics",
        adapter="api",
        description="observations POSTed to /api/v1/rollouts/{id}/observations",
        features=frozenset({"push"}),
    ),
    factory=_api,
)

PROMETHEUS = AdapterSpec(
    capability=Capability(
        port="rollout_metrics",
        adapter="prometheus",
        description="PromQL range queries per metric over each arm's window",
        features=frozenset({"pull"}),
        config_keys=(
            "rollout_prometheus_url", "rollout_prometheus_token", "rollout_prometheus_queries",
            "rollout_prometheus_requests_query", "rollout_prometheus_step_s",
            "rollout_prometheus_timeout_s",
        ),
        required_keys=("rollout_prometheus_url", "rollout_prometheus_queries"),
    ),
    factory=_prometheus,
)
