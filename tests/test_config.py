from naukri_agent.config import LLMProviderName, Settings


def test_defaults_load_without_env_file():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.dry_run is True
    assert settings.auto_apply is False


def test_llm_provider_and_model_are_independent():
    """
    Changing the provider must not implicitly change or validate the
    model string against a hard-coded list — that would recreate the
    coupling we're explicitly avoiding.
    """
    settings = Settings(
        _env_file=None,
        llm_provider="groq",
        llm_model="llama-3.1-70b-versatile",
    )
    assert settings.llm_provider == LLMProviderName.GROQ
    assert settings.llm_model == "llama-3.1-70b-versatile"


def test_ollama_provider_configured_by_host_only():
    settings = Settings(_env_file=None, llm_provider="ollama", ollama_host="http://localhost:11434")
    assert settings.llm_provider_is_configured() is True


def test_groq_provider_requires_api_key():
    settings = Settings(_env_file=None, llm_provider="groq", groq_api_key="")
    assert settings.llm_provider_is_configured() is False

    settings = Settings(_env_file=None, llm_provider="groq", groq_api_key="gsk_fake")
    assert settings.llm_provider_is_configured() is True


def test_invalid_log_level_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="NOT_A_LEVEL")
