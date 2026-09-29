# Rollout metrics adapter template

A starting point for a `RolloutMetricsPort` adapter (docs/adapters/rollout_metrics.md): where a
canary / A/B / shadow rollout reads each arm's online health metrics. `adapter.py` reads
observations from a JSON-lines file the serving layer appends to; replace `_lines()` with a
query to your metrics store (a time-series database, a log index, a vendor API; import its SDK
inside the adapter, never at module level of the core).

Run the conformance suite against your adapter before registering it:

```python
import pytest
from oran_adapt.conformance.rollout_metrics import CHECKS, Context

@pytest.mark.parametrize("check", sorted(CHECKS))
def test_conformance(check, tmp_path):
    path = tmp_path / "obs.jsonl"
    def seed(window, at, requests, metrics):
        with path.open("a") as f:
            f.write(json.dumps({"rollout_id": window.rollout_id, "arm": window.arm,
                                "at": at.isoformat(), "requests": requests,
                                "metrics": metrics}) + "\n")
    CHECKS[check](JsonlRolloutMetrics(str(path)), Context(session=None, seed=seed))
```

`tests/unit/test_phase7_gate_delivery.py` runs exactly this against the template, so it stays
conformant.
