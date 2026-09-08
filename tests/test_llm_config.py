import pytest
from pydantic import ValidationError

from naukri_agent.config import LLMProviderName, Settings


@pytest.mark.parametrize("provider", ["openai", "groq", "ollama"])
def test_valid_providers_accepted(provider):
    settings = Settings(_env_file=None, llm_provider=provider)
    assert settings.llm_provider == LLMProviderName(provider)


def test_unknown_provider_rejected():
    """
    A model name (e.g. 'llama3.1') must never be accepted as a
    provider — this is the exact confusion the provider/model split
    exists to prevent.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="llama3.1")
