"""An LLM provider adapter template: a JSON endpoint that takes a system prompt and a user turn.

The wire format it assumes (replace it with your provider's)::

    POST {base_url}/generate
    {"system": "...", "prompt": "...", "max_tokens": 1024}
    -> {"text": "...", "usage": {"input_tokens": 12, "output_tokens": 34}}

Keep the rules of docs/adapters/llm.md:

- every failure is LlmUnavailableError, and none repeats the request or the API key;
- the HTTP client comes from the outbound policy (``policy.client``), never ``httpx.Client``;
- report the provider's token counts with ``report_usage`` (the guard's budget uses them);
- no retries of its own (the guard retries, backs off and opens the circuit breaker);
- it pickles to its configuration (a job worker process receives the resolved instance).

Register it in your package's ``pyproject.toml``::

    [project.entry-points."oran_adapt.llm"]
    my-llm = "my_package.adapter:SPEC"

then set ``LLM_ENABLED=true``, ``LLM_PROVIDER=my-llm``, ``MY_LLM_BASE_URL`` and
``MY_LLM_API_KEY``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from oran_adapt.core.errors import LlmUnavailableError, OutboundBlockedError
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.llm.calls import report_usage
from oran_adapt.ports import AdapterSpec, Capability

PROVIDER = "my-llm"


class JsonLlmClient:
    PATH = "/generate"

    def __init__(self, base_url: str, *, timeout_s: float, max_tokens: int,
                 policy: OutboundPolicy, api_key: str | None = None) -> None:
        self._config = {"base_url": base_url.rstrip("/"), "timeout_s": timeout_s,
                        "max_tokens": max_tokens, "policy": policy, "api_key": api_key}
        self._client = policy.client(timeout=timeout_s)

    def __getstate__(self) -> dict[str, Any]:
        return dict(self._config)

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(state.pop("base_url"), **state)  # type: ignore[misc]

    def _fail(self, message: str, exc: BaseException | None = None,
              **context: Any) -> LlmUnavailableError:
        if exc is not None:
            context["cause"] = type(exc).__name__  # the type only: never the request or key
        return LlmUnavailableError(message, provider=PROVIDER, **context)

    def complete(self, *, system: str, prompt: str) -> str:
        cfg = self._config
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        body = {"system": system, "prompt": prompt, "max_tokens": cfg["max_tokens"]}
        try:
            response = self._client.post(cfg["base_url"] + self.PATH, json=body,
                                         headers=headers)
        except (OutboundBlockedError, httpx.HTTPError) as exc:
            raise self._fail("the completion request failed", exc) from exc
        if response.status_code >= 400:
            raise self._fail("the completion was refused", status=response.status_code)
        try:
            payload = response.json()
            text = payload["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise self._fail("the response is not in the expected shape", exc) from exc
        usage = payload.get("usage")
        if isinstance(usage, dict):
            report_usage(usage.get("input_tokens") or 0, usage.get("output_tokens") or 0)
        if not isinstance(text, str) or not text:
            raise self._fail("the response carries no text")
        return text


def _factory(settings: Any) -> JsonLlmClient:
    # Settings ignores unknown keys; a plugin reads its own from the environment.
    base_url = os.environ.get("MY_LLM_BASE_URL")
    if not base_url:
        raise LlmUnavailableError("MY_LLM_BASE_URL is not set", key="MY_LLM_BASE_URL")
    return JsonLlmClient(
        base_url, timeout_s=settings.llm_timeout_s, max_tokens=settings.llm_max_output_tokens,
        policy=OutboundPolicy.from_settings(settings),
        api_key=os.environ.get("MY_LLM_API_KEY"),
    )


SPEC = AdapterSpec(
    capability=Capability(
        port="llm",
        adapter=PROVIDER,
        description="a JSON /generate endpoint (template)",
        features=frozenset({"completion", "system_prompt", "network", "usage"}),
        config_keys=("llm_timeout_s", "llm_max_output_tokens"),
    ),
    factory=_factory,
)
