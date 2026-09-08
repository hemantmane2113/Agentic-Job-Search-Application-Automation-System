import pytest

from naukri_agent.config import LLMProviderName, Settings
from naukri_agent.llm.exceptions import LLMConfigError
from naukri_agent.llm.factory import build_llm_provider, get_default_llm_provider
from naukri_agent.llm.providers.groq_provider import GroqProvider
from naukri_agent.llm.providers.ollama_provider import OllamaProvider
from naukri_agent.llm.providers.openai_provider import OpenAIProvider


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_build_ollama_provider():
    settings = _settings(ollama_host="http://localhost:11434")
    provider = build_llm_provider(LLMProviderName.OLLAMA, "llama3.1", settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1"
    assert provider.host == "http://localhost:11434"


def test_build_groq_provider_with_api_key():
    settings = _settings(groq_api_key="gsk_fake")
    provider = build_llm_provider(LLMProviderName.GROQ, "llama-3.1-70b-versatile", settings)
    assert isinstance(provider, GroqProvider)
    assert provider.model == "llama-3.1-70b-versatile"


def test_build_groq_provider_without_api_key_raises():
    settings = _settings(groq_api_key="")
    with pytest.raises(LLMConfigError):
        build_llm_provider(LLMProviderName.GROQ, "m", settings)


def test_build_openai_provider_with_api_key():
    settings = _settings(openai_api_key="sk-fake")
    provider = build_llm_provider(LLMProviderName.OPENAI, "gpt-4o-mini", settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-4o-mini"


def test_build_openai_provider_without_api_key_raises():
    settings = _settings(openai_api_key="")
    with pytest.raises(LLMConfigError):
        build_llm_provider(LLMProviderName.OPENAI, "m", settings)


def test_provider_and_model_are_independent_parameters():
    """
    The same provider can be asked for any model string -- there is
    no hard-coded provider -> model mapping anywhere in the factory.
    """
    settings = _settings(ollama_host="http://x")
    p1 = build_llm_provider(LLMProviderName.OLLAMA, "llama3.1", settings)
    p2 = build_llm_provider(LLMProviderName.OLLAMA, "qwen2.5", settings)
    assert p1.model == "llama3.1"
    assert p2.model == "qwen2.5"
    assert type(p1) is type(p2)


def test_get_default_provider_uses_settings_llm_provider_and_model():
    settings = _settings(llm_provider="ollama", llm_model="llama3.1", ollama_host="http://x")
    provider = get_default_llm_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1"


def test_switching_provider_via_settings_changes_provider_type():
    settings_ollama = _settings(llm_provider="ollama", ollama_host="http://x")
    settings_groq = _settings(llm_provider="groq", groq_api_key="gsk_fake")

    provider_a = get_default_llm_provider(settings_ollama)
    provider_b = get_default_llm_provider(settings_groq)

    assert isinstance(provider_a, OllamaProvider)
    assert isinstance(provider_b, GroqProvider)


def test_different_task_can_use_different_model_without_changing_settings():
    """
    Section 9's requirement: a cheap/local model for one task and a
    stronger one for another, without hard-coding one model
    throughout the application -- build_llm_provider takes the model
    as an explicit parameter, independent of Settings.llm_model.
    """
    settings = _settings(ollama_host="http://x")
    cheap = build_llm_provider(LLMProviderName.OLLAMA, "llama3.1", settings)
    strong = build_llm_provider(LLMProviderName.OLLAMA, "llama3.1:70b", settings)
    assert cheap.model != strong.model
