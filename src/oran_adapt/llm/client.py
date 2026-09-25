"""Provider-agnostic LLM client used wherever the pipeline needs a model's judgement (Member 2
decision, and later Member 3's LLM adaptation adapters). Callers never talk to the Anthropic or
Gemini SDKs directly - they go through this so a provider outage is a typed error, not a crash.
"""

from __future__ import annotations

from typing import Protocol

from oran_adapt.core import metrics
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import LlmUnavailableError


class LlmClient(Protocol):
    """A single-turn text completion call. Implementations raise LlmUnavailableError on any
    failure (auth, network, timeout, provider error) - never let a provider SDK exception
    escape this boundary."""

    def complete(self, *, system: str, prompt: str) -> str: ...


class AnthropicLlmClient:
    def __init__(self, api_key: str, model: str, timeout_s: float) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self._model = model

    def complete(self, *, system: str, prompt: str) -> str:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise LlmUnavailableError(
                "Anthropic completion failed", provider="anthropic", cause=str(exc)
            ) from exc
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        if not text:
            raise LlmUnavailableError(
                "Anthropic returned no text content", provider="anthropic"
            )
        return text


class GeminiLlmClient:
    def __init__(self, api_key: str, model: str, timeout_s: float) -> None:
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._http_options = types.HttpOptions(timeout=int(timeout_s * 1000))

    def complete(self, *, system: str, prompt: str) -> str:
        from google.genai import errors as genai_errors
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system, http_options=self._http_options
                ),
            )
        except genai_errors.APIError as exc:
            raise LlmUnavailableError(
                "Gemini completion failed", provider="gemini", cause=str(exc)
            ) from exc
        text = response.text
        if not text:
            raise LlmUnavailableError("Gemini returned no text content", provider="gemini")
        return text


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


def build_llm_client(settings: Settings) -> LlmClient | None:
    """None means "no LLM configured" (LLM_PROVIDER=none) - callers must have a deterministic
    fallback for that case, not treat it as an error."""
    if settings.llm_provider == "anthropic":
        assert settings.anthropic_api_key is not None  # enforced by Settings validator
        return InstrumentedLlmClient(
            AnthropicLlmClient(
                settings.anthropic_api_key.get_secret_value(),
                settings.anthropic_model,
                settings.llm_timeout_s,
            ),
            "anthropic",
        )
    if settings.llm_provider == "gemini":
        assert settings.gemini_api_key is not None  # enforced by Settings validator
        return InstrumentedLlmClient(
            GeminiLlmClient(
                settings.gemini_api_key.get_secret_value(),
                settings.gemini_model,
                settings.llm_timeout_s,
            ),
            "gemini",
        )
    return None
