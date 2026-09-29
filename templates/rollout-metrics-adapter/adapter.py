"""A rollout metrics adapter template: reads each arm's online metrics from a JSON-lines file.

Every line is one observation, written by the serving layer::

    {"rollout_id": "...", "arm": "candidate", "at": "2030-01-01T00:05:00+00:00",
     "requests": 50, "metrics": {"error_rate": 0.01, "latency_p95_ms": 42.0}}

Replace ``_lines()`` with a query to your metrics store; keep ``observe``'s rules
(docs/adapters/rollout_metrics.md): only the window's arm, only observations inside
``[start, end]``, finite floats, and RolloutMetricsUnavailableError when the source is down.

Register it in your package's ``pyproject.toml``::

    [project.entry-points."oran_adapt.rollout_metrics"]
    jsonl = "my_package.adapter:SPEC"

then set ``ROLLOUT_METRICS_BACKEND=jsonl`` and ``ROLLOUT_JSONL_PATH``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from oran_adapt.core.errors import RolloutMetricsUnavailableError
from oran_adapt.ports import AdapterSpec, ArmStats, ArmWindow, Capability


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class JsonlRolloutMetrics:
    def __init__(self, path: str) -> None:
        self.path = path

    def _lines(self) -> Iterator[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise RolloutMetricsUnavailableError(
                "the observations file could not be read", path=self.path, cause=str(exc)
            ) from exc

    def ping(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(directory):
            raise RolloutMetricsUnavailableError(
                "the observations directory does not exist", path=self.path
            )

    def observe(self, session: Any, window: ArmWindow) -> ArmStats:
        self.ping()
        start, end = _aware(window.start), _aware(window.end)
        samples: dict[str, list[float]] = {}
        count = 0
        for row in self._lines():
            if row.get("rollout_id") != window.rollout_id or row.get("arm") != window.arm:
                continue
            at = _aware(datetime.fromisoformat(row["at"]))
            if not start <= at <= end:
                continue
            count += int(row.get("requests", 1))
            for name, value in (row.get("metrics") or {}).items():
                if isinstance(value, int | float) and math.isfinite(float(value)):
                    samples.setdefault(name, []).append(float(value))
        return ArmStats(samples=samples, count=count)


def _factory(settings: Any) -> JsonlRolloutMetrics:
    # Settings ignores unknown keys; a plugin reads its own from the environment.
    return JsonlRolloutMetrics(os.environ.get("ROLLOUT_JSONL_PATH", "rollout-observations.jsonl"))


SPEC = AdapterSpec(
    capability=Capability(
        port="rollout_metrics",
        adapter="jsonl",
        description="observations appended to a JSON-lines file by the serving layer",
        features=frozenset({"pull"}),
    ),
    factory=_factory,
)
