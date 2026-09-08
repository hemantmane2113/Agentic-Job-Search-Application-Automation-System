from naukri_agent.config import Settings
from naukri_agent.llm.factory import get_default_llm_provider, get_resume_llm_provider
from naukri_agent.llm.providers.groq_provider import GroqProvider
from naukri_agent.llm.providers.ollama_provider import OllamaProvider


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_resume_provider_falls_back_to_default_when_unset():
    settings = _settings(llm_provider="ollama", llm_model="llama3.1", ollama_host="http://x")
    provider = get_resume_llm_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1"


def test_resume_provider_uses_override_when_set():
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        resume_llm_provider="groq",
        resume_llm_model="llama-3.1-70b-versatile",
        groq_api_key="gsk_fake",
    )
    provider = get_resume_llm_provider(settings)
    assert isinstance(provider, GroqProvider)
    assert provider.model == "llama-3.1-70b-versatile"


def test_job_parser_and_resume_classifier_can_use_different_providers_simultaneously():
    """
    JobParser stays on the cheap/local default while the resume
    role-classification assist (resume/selector.py) uses a different
    provider, with no coupling between the two.
    """
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        resume_llm_provider="groq",
        resume_llm_model="llama-3.1-70b-versatile",
        groq_api_key="gsk_fake",
    )
    job_parser_provider = get_default_llm_provider(settings)
    resume_provider = get_resume_llm_provider(settings)

    assert isinstance(job_parser_provider, OllamaProvider)
    assert isinstance(resume_provider, GroqProvider)
    assert job_parser_provider.model != resume_provider.model


def test_partial_override_only_model_falls_back_provider():
    settings = _settings(
        llm_provider="ollama",
        llm_model="llama3.1",
        ollama_host="http://x",
        resume_llm_model="llama3.1:70b",  # only model overridden
    )
    provider = get_resume_llm_provider(settings)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.1:70b"
