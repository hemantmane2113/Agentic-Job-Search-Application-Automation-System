from naukri_agent.config import Settings
from naukri_agent.llm.factory import get_apply_llm_provider, get_default_llm_provider
from naukri_agent.llm.providers.groq_provider import GroqProvider
from naukri_agent.llm.providers.ollama_provider import OllamaProvider


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_apply_provider_falls_back_to_default_when_unset():
    settings = _settings(llm_provider="ollama", llm_model="llama3.1", ollama_host="http://x")
    provider = get_apply_llm_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1"


def test_apply_provider_uses_override_when_set():
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        apply_llm_provider="groq",
        apply_llm_model="llama-3.1-70b-versatile",
        groq_api_key="gsk_fake",
    )
    provider = get_apply_llm_provider(settings)
    assert isinstance(provider, GroqProvider)
    assert provider.model == "llama-3.1-70b-versatile"


def test_job_parser_and_apply_drafter_can_use_different_providers_simultaneously():
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        apply_llm_provider="groq",
        apply_llm_model="llama-3.1-70b-versatile",
        groq_api_key="gsk_fake",
    )
    job_parser_provider = get_default_llm_provider(settings)
    apply_provider = get_apply_llm_provider(settings)

    assert isinstance(job_parser_provider, OllamaProvider)
    assert isinstance(apply_provider, GroqProvider)


def test_partial_override_only_model_falls_back_provider():
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        apply_llm_model="llama3.1:70b",
    )
    provider = get_apply_llm_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1:70b"
