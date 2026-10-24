"""The LLM provider follows the API key that is set (LLM_PROVIDER unset or "auto")."""

from __future__ import annotations

import pytest

from oran_adapt.core.config import LLM_DEFAULT_MODELS, OPENAI_DEFAULT_BASE_URL, Settings

KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "LLM_PROVIDER", "ANTHROPIC_MODEL", "LLM_OPENAI_MODEL", "LLM_OPENAI_BASE_URL")


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_no_key_keeps_the_default_provider(clean_env: pytest.MonkeyPatch) -> None:
    s = _settings()
    assert s.llm_provider == "gemini" and s.gemini_api_key is None


@pytest.mark.parametrize("env, provider", [
    ({"GEMINI_API_KEY": "g"}, "gemini"),
    ({"OPENAI_API_KEY": "o"}, "openai-compatible"),
    ({"LLM_OPENAI_API_KEY": "o"}, "openai-compatible"),
    ({"ANTHROPIC_API_KEY": "a"}, "anthropic"),
    ({"LLM_PROVIDER": "auto", "ANTHROPIC_API_KEY": "a"}, "anthropic"),
])
def test_the_key_that_is_set_picks_the_provider(clean_env: pytest.MonkeyPatch,
                                                env: dict[str, str], provider: str) -> None:
    for key, value in env.items():
        clean_env.setenv(key, value)
    assert _settings().llm_provider == provider


def test_openai_key_gets_the_openai_endpoint_and_a_model(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("OPENAI_API_KEY", "o")
    s = _settings()
    assert s.llm_openai_base_url == OPENAI_DEFAULT_BASE_URL
    assert s.llm_openai_model == LLM_DEFAULT_MODELS["openai-compatible"]
    assert s.llm_openai_api_key is not None and s.llm_openai_api_key.get_secret_value() == "o"


def test_anthropic_key_gets_a_model_unless_one_is_set(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    assert _settings().anthropic_model == LLM_DEFAULT_MODELS["anthropic"]
    clean_env.setenv("ANTHROPIC_MODEL", "my-model")
    assert _settings().anthropic_model == "my-model"


def test_order_when_several_keys_are_set(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    clean_env.setenv("OPENAI_API_KEY", "o")
    assert _settings().llm_provider == "openai-compatible"
    clean_env.setenv("GEMINI_API_KEY", "g")
    assert _settings().llm_provider == "gemini"


def test_a_named_provider_is_kept(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("GEMINI_API_KEY", "g")
    clean_env.setenv("LLM_PROVIDER", "none")
    assert _settings().llm_provider == "none"
