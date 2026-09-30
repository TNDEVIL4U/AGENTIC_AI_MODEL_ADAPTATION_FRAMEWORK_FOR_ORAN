# LLM adapter template

A starting point for an `LLMPort` adapter (docs/adapters/llm.md). `adapter.py` calls a JSON
`POST {base_url}/generate` endpoint. Replace the request body and the response parsing with
your provider's wire format, and import its SDK inside the adapter, never in the core.

The adapter gets its HTTP client from the outbound policy. That policy trusts the endpoints the
framework's own settings name, so if your endpoint's host is private, add it to
`OUTBOUND_ALLOWLIST`.

`build_llm` wraps the adapter in the guard, which adds the caps, budget, retries and circuit
breaker. Do not add any of these to the adapter.

Run the conformance suite against your adapter before you register it:

```python
import pytest
from oran_adapt.conformance.llm import LLM_CHECKS, LlmContext

@pytest.mark.parametrize("check", sorted(LLM_CHECKS))
def test_conformance(check):
    port = JsonLlmClient(url, timeout_s=1, max_tokens=64, policy=policy, api_key=KEY)
    ctx = LlmContext(unreachable=lambda: JsonLlmClient(dead_url, timeout_s=1, max_tokens=64,
                                                       policy=policy, api_key=KEY), secret=KEY)
    LLM_CHECKS[check](port, ctx)
```

`tests/unit/test_phase10_llm.py::test_template_adapter_conformance` runs exactly this against a
local double of the template's format, which keeps the template conformant.
