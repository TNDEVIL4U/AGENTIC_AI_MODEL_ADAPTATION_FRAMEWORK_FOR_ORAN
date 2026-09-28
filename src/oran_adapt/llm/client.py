"""The LLM seen from the pipeline: wherever it needs a model's judgement (Member 2 decision, and
Member 3's LLM adaptation adapters) it calls an ``LlmClient`` - the LLMPort - and never a provider
SDK, so a provider outage is a typed error, not a crash. Provider adapters live in
oran_adapt.adapters.llm_providers and are chosen by LLM_PROVIDER at the composition root
(oran_adapt.bootstrap.build_llm), which wraps them in InstrumentedLlmClient.
"""

from __future__ import annotations

from oran_adapt.core import metrics
from oran_adapt.ports import LLMPort

LlmClient = LLMPort


class InstrumentedLlmClient:
    """Counts every call (``llm_requests_total``) and every failed one (``llm_failures_total``)
    by provider, then passes the result or the error through unchanged."""

    def __init__(self, inner: LlmClient, provider: str) -> None:
        self.inner = inner
        self.provider = provider

    def complete(self, *, system: str, prompt: str) -> str:
        metrics.LLM_REQUESTS.labels(self.provider).inc()
        try:
            return self.inner.complete(system=system, prompt=prompt)
        except Exception:
            metrics.LLM_FAILURES.labels(self.provider).inc()
            raise
