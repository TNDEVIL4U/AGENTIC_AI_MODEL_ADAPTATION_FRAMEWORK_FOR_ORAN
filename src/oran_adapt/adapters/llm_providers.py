"""LLM adapters ``anthropic``, ``gemini`` and ``openai-compatible``.

Each turns every provider failure into LlmUnavailableError, so no SDK exception crosses the
LLMPort, and none of those errors carries the request or the API key: only the provider, the
exception type and the HTTP status. Each reports the token counts the provider returned
(llm.calls.report_usage) for the guard's budget. None retries on its own: the guard (llm.guard)
owns retries, backoff and the circuit breaker.

Every request goes through the outbound policy (core/outbound.py): the SDKs are handed a
policy-checked httpx client, so an endpoint outside OUTBOUND_ALLOWLIST is refused before
anything is sent. All three pickle to their configuration and rebuild the client on unpickling,
so a job worker process can receive the resolved instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from oran_adapt.core.errors import LlmUnavailableError, OutboundBlockedError
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.llm.calls import report_usage
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

    def _http(self, module: Any = httpx) -> Any:
        policy: OutboundPolicy = self._config["policy"]
        return policy.client_from(module, timeout=self._config["timeout_s"])


def _unavailable(provider: str, message: str, exc: BaseException | None = None,
                 **context: Any) -> LlmUnavailableError:
    if exc is not None:
        context["cause"] = type(exc).__name__
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status, int):
            context["status"] = status
    return LlmUnavailableError(message, provider=provider, **context)


class AnthropicLlmClient(_RebuildOnUnpickle):
    def __init__(
        self, api_key: str, model: str, timeout_s: float, *, max_tokens: int,
        policy: OutboundPolicy, base_url: str | None = None,
    ) -> None:
        self._config = {
            "api_key": api_key,
            "model": model,
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "policy": policy,
            "base_url": base_url,
        }
        self._connect()

    def _connect(self) -> None:
        import anthropic
        import httpx2  # the Anthropic SDK's own httpx fork

        cfg = self._config
        self._client = anthropic.Anthropic(
            api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=cfg["timeout_s"],
            max_retries=0, http_client=self._http(httpx2),
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
        except (anthropic.AnthropicError, OutboundBlockedError, httpx.HTTPError) as exc:
            raise _unavailable("anthropic", "Anthropic completion failed", exc) from exc
        usage = getattr(response, "usage", None)
        if usage is not None:
            report_usage(getattr(usage, "input_tokens", 0) or 0,
                         getattr(usage, "output_tokens", 0) or 0)
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        if not text:
            raise _unavailable("anthropic", "Anthropic returned no text content")
        return text


class GeminiLlmClient(_RebuildOnUnpickle):
    def __init__(
        self, api_key: str, model: str, timeout_s: float, *, max_tokens: int,
        policy: OutboundPolicy, base_url: str | None = None,
    ) -> None:
        self._config = {
            "api_key": api_key,
            "model": model,
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "policy": policy,
            "base_url": base_url,
        }
        self._connect()

    def _connect(self) -> None:
        from google import genai
        from google.genai import types

        cfg = self._config
        # One attempt: the guard retries.
        self._http_options = types.HttpOptions(
            base_url=cfg["base_url"],
            timeout=int(cfg["timeout_s"] * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
            httpx_client=self._http(),
        )
        self._client = genai.Client(api_key=cfg["api_key"], http_options=self._http_options)

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
                    # Plain completion: no tools, so no automatic function-calling loop.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True),
                ),
            )
        except (genai_errors.APIError, OutboundBlockedError, httpx.HTTPError) as exc:
            raise _unavailable("gemini", "Gemini completion failed", exc) from exc
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            report_usage(getattr(usage, "prompt_token_count", 0) or 0,
                         getattr(usage, "candidates_token_count", 0) or 0)
        text = response.text
        if not text:
            raise _unavailable("gemini", "Gemini returned no text content")
        return text


class OpenAICompatibleLlmClient(_RebuildOnUnpickle):
    """``POST {base_url}/chat/completions`` in the OpenAI wire format, which vLLM, Ollama,
    LM Studio, llama.cpp's server, LiteLLM and most gateways serve. Plain httpx: no SDK."""

    PATH = "/chat/completions"

    def __init__(
        self, base_url: str, model: str, timeout_s: float, *, max_tokens: int,
        policy: OutboundPolicy, api_key: str | None = None,
    ) -> None:
        self._config = {
            "base_url": base_url.rstrip("/"),
            "model": model,
            "timeout_s": timeout_s,
            "max_tokens": max_tokens,
            "policy": policy,
            "api_key": api_key,
        }
        self._connect()

    def _connect(self) -> None:
        self._client = self._http()

    def complete(self, *, system: str, prompt: str) -> str:
        cfg = self._config
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        body = {
            "model": cfg["model"],
            "max_tokens": cfg["max_tokens"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
        }
        try:
            response = self._client.post(cfg["base_url"] + self.PATH, json=body, headers=headers)
        except (OutboundBlockedError, httpx.HTTPError) as exc:
            raise _unavailable("openai-compatible", "chat completion failed", exc) from exc
        if response.status_code >= 400:
            raise _unavailable("openai-compatible", "chat completion was refused",
                               status=response.status_code)
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _unavailable("openai-compatible",
                               "chat completion response is not in the expected shape",
                               exc) from exc
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            report_usage(usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)
        if not isinstance(text, str) or not text:
            raise _unavailable("openai-compatible", "chat completion returned no text")
        return text


def _secret(settings: Settings, key: str) -> str:
    value = getattr(settings, key)
    if value is None:  # the config loader enforces required_keys before any factory runs
        raise LlmUnavailableError(f"{key.upper()} is not set", key=key.upper())
    return str(value.get_secret_value())


def _model(settings: Settings, key: str) -> str:
    value = getattr(settings, key)
    if not value:
        raise LlmUnavailableError(f"{key.upper()} is not set", key=key.upper())
    return str(value)


def _common(settings: Settings) -> dict[str, Any]:
    return {"max_tokens": settings.llm_max_output_tokens,
            "policy": OutboundPolicy.from_settings(settings)}


def _anthropic(settings: Settings) -> AnthropicLlmClient:
    return AnthropicLlmClient(
        _secret(settings, "anthropic_api_key"), _model(settings, "anthropic_model"),
        settings.llm_timeout_s,
        base_url=settings.anthropic_base_url, **_common(settings),
    )


def _gemini(settings: Settings) -> GeminiLlmClient:
    return GeminiLlmClient(
        _secret(settings, "gemini_api_key"), _model(settings, "gemini_model"),
        settings.llm_timeout_s,
        base_url=settings.gemini_base_url, **_common(settings),
    )


def _openai_compatible(settings: Settings) -> OpenAICompatibleLlmClient:
    if not settings.llm_openai_base_url or not settings.llm_openai_model:
        raise LlmUnavailableError("LLM_OPENAI_BASE_URL and LLM_OPENAI_MODEL must be set",
                                  key="LLM_OPENAI_BASE_URL")
    key = settings.llm_openai_api_key
    return OpenAICompatibleLlmClient(
        settings.llm_openai_base_url, settings.llm_openai_model, settings.llm_timeout_s,
        api_key=key.get_secret_value() if key is not None else None, **_common(settings),
    )


_COMMON_KEYS = ("llm_timeout_s", "llm_max_output_tokens")

ANTHROPIC = AdapterSpec(
    capability=Capability(
        port="llm",
        adapter="anthropic",
        description="Anthropic Messages API",
        features=frozenset({"completion", "system_prompt", "network", "usage"}),
        config_keys=("anthropic_api_key", "anthropic_model", "anthropic_base_url",
                     *_COMMON_KEYS),
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
        features=frozenset({"completion", "system_prompt", "network", "usage"}),
        config_keys=("gemini_api_key", "gemini_model", "gemini_base_url", *_COMMON_KEYS),
        required_keys=("gemini_api_key", "gemini_model"),
        distributions=("google-genai",),
    ),
    factory=_gemini,
)

OPENAI_COMPATIBLE = AdapterSpec(
    capability=Capability(
        port="llm",
        adapter="openai-compatible",
        description="Any OpenAI-compatible /chat/completions endpoint, including self-hosted",
        features=frozenset({"completion", "system_prompt", "network", "usage", "self_hosted"}),
        config_keys=("llm_openai_base_url", "llm_openai_model", "llm_openai_api_key",
                     *_COMMON_KEYS),
        required_keys=("llm_openai_base_url", "llm_openai_model"),
    ),
    factory=_openai_compatible,
)
