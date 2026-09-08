"""
Factory functions for constructing an LLMProvider.

build_llm_provider() takes the provider and model as explicit
parameters rather than always reading the global default — this is
what lets different agents use different models for different tasks
(e.g. a cheap/local model for job parsing, a stronger one for resume
tailoring) without any code changes here, per Section 9's requirement.
get_default_llm_provider() is a convenience wrapper for the common
case of "just use whatever LLM_PROVIDER/LLM_MODEL say".
"""

from __future__ import annotations

from naukri_agent.config import LLMProviderName, Settings
from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMConfigError
from naukri_agent.llm.providers.groq_provider import GroqProvider
from naukri_agent.llm.providers.ollama_provider import OllamaProvider
from naukri_agent.llm.providers.openai_provider import OpenAIProvider


def build_llm_provider(
    provider: LLMProviderName, model: str, settings: Settings
) -> LLMProvider:
    """
    Construct a provider for the given (provider, model) pair.
    `provider` says WHO to call; `model` says WHICH model to ask that
    provider for — the two are never conflated here or anywhere else.
    """
    if provider == LLMProviderName.OLLAMA:
        return OllamaProvider(
            model=model, host=settings.ollama_host, timeout=settings.llm_timeout_seconds
        )

    if provider == LLMProviderName.GROQ:
        if not settings.groq_api_key:
            raise LLMConfigError("GROQ_API_KEY is not set")
        return GroqProvider(
            model=model, api_key=settings.groq_api_key, timeout=settings.llm_timeout_seconds
        )

    if provider == LLMProviderName.OPENAI:
        if not settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        return OpenAIProvider(
            model=model, api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds
        )

    raise LLMConfigError(f"Unknown LLM provider: {provider!r}")


def get_default_llm_provider(settings: Settings) -> LLMProvider:
    """Build a provider from LLM_PROVIDER/LLM_MODEL — the common case."""
    return build_llm_provider(settings.llm_provider, settings.llm_model, settings)


def get_resume_llm_provider(settings: Settings) -> LLMProvider:
    """
    Build the provider used for resume role-classification assistance
    (resume/selector.py's classify_role_with_llm) — never for
    generating resume content, since nothing in this system does that.
    Falls back to the default LLM_PROVIDER/LLM_MODEL when
    RESUME_LLM_PROVIDER/RESUME_LLM_MODEL aren't set.
    """
    provider = settings.resume_llm_provider or settings.llm_provider
    model = settings.resume_llm_model or settings.llm_model
    return build_llm_provider(provider, model, settings)
