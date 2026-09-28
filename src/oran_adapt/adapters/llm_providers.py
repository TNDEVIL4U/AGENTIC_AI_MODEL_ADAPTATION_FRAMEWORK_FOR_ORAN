"""LLM adapters ``anthropic`` and ``gemini``. Each turns every provider SDK failure into
LlmUnavailableError, so no SDK exception crosses the LLMPort. Both pickle to their
configuration and rebuild the SDK client on unpickling, so a job worker process can receive the
resolved instance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import LlmUnavailableError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings


class _RebuildOnUnpickle:
    """Pickles to ``_config`` and calls ``_connect`` again when unpickled."""

    _config: dict[str, Any]

    def _connect(self) -> None:
        raise TypeError(f"{type(self).__name__} must define _connect")

    def __getstate__(self) -> dict[str, Any]:
        return dict(self._config)

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._config = state
        self._connect()


class AnthropicLlmClient(_RebuildOnUnpickle):
    def __init__(
        self, api_key: str, model: str, timeout_s: float, *, max_tokens: int, max_retries: int
    ) -> None:
        self._config = {
            "api_key": api_key,
            "model": model,
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "max_retries": max_retries,
        }
        self._connect()

    def _connect(self) -> None:
        import anthropic

        cfg = self._config
        self._client = anthropic.Anthropic(
            api_key=cfg["api_key"], timeout=cfg["timeout_s"], max_retries=cfg["max_retries"]
        )

    def complete(self, *, system: str, prompt: str) -> str:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._config["model"],
                max_tokens=self._config["max_tokens"],
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
            raise LlmUnavailableError("Anthropic returned no text content", provider="anthropic")
        return text


class GeminiLlmClient(_RebuildOnUnpickle):
    def __init__(
        self, api_key: str, model: str, timeout_s: float, *, max_tokens: int, max_retries: int
    ) -> None:
        self._config = {
            "api_key": api_key,
            "model": model,
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "max_retries": max_retries,
        }
        self._connect()

    def _connect(self) -> None:
        from google import genai
        from google.genai import types

        cfg = self._config
        self._client = genai.Client(api_key=cfg["api_key"])
        # max_retries counts retries after the first attempt; genai counts attempts.
        self._http_options = types.HttpOptions(
            timeout=int(cfg["timeout_s"] * 1000),
            retry_options=types.HttpRetryOptions(attempts=cfg["max_retries"] + 1),
        )

    def complete(self, *, system: str, prompt: str) -> str:
        from google.genai import errors as genai_errors
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=self._config["model"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=self._config["max_tokens"],
                    http_options=self._http_options,
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


def _secret(settings: Settings, key: str) -> str:
    value = getattr(settings, key)
    if value is None:  # the config loader enforces required_keys before any factory runs
        raise LlmUnavailableError(f"{key.upper()} is not set", key=key.upper())
    return str(value.get_secret_value())


def _anthropic(settings: Settings) -> AnthropicLlmClient:
    return AnthropicLlmClient(
        _secret(settings, "anthropic_api_key"),
        settings.anthropic_model,
        settings.llm_timeout_s,
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_max_retries,
    )


def _gemini(settings: Settings) -> GeminiLlmClient:
    return GeminiLlmClient(
        _secret(settings, "gemini_api_key"),
        settings.gemini_model,
        settings.llm_timeout_s,
        max_tokens=settings.llm_max_output_tokens,
        max_retries=settings.llm_max_retries,
    )


_COMMON_KEYS = ("llm_timeout_s", "llm_max_output_tokens", "llm_max_retries")

ANTHROPIC = AdapterSpec(
    capability=Capability(
        port="llm",
        adapter="anthropic",
        description="Anthropic Messages API",
        features=frozenset({"completion", "system_prompt", "network"}),
        config_keys=("anthropic_api_key", "anthropic_model", *_COMMON_KEYS),
        required_keys=("anthropic_api_key", "anthropic_model"),
        distributions=("anthropic",),
    ),
    factory=_anthropic,
)

GEMINI = AdapterSpec(
    capability=Capability(
        port="llm",
        adapter="gemini",
        description="Google Gemini generate_content API",
        features=frozenset({"completion", "system_prompt", "network"}),
        config_keys=("gemini_api_key", "gemini_model", *_COMMON_KEYS),
        required_keys=("gemini_api_key", "gemini_model"),
        distributions=("google-genai",),
    ),
    factory=_gemini,
)
