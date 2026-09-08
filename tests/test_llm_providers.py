import pytest

from naukri_agent.llm.exceptions import LLMRequestError, LLMTimeoutError
from naukri_agent.llm.providers.groq_provider import GroqProvider
from naukri_agent.llm.providers.ollama_provider import OllamaProvider
from naukri_agent.llm.providers.openai_provider import OpenAIProvider


# --- Ollama ---


class FakeOllamaClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.last_kwargs = None

    def chat(self, **kwargs):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


def test_ollama_provider_returns_content():
    client = FakeOllamaClient(response={"message": {"content": "hello"}})
    provider = OllamaProvider(model="llama3.1", host="http://localhost:11434", client=client)
    assert provider.complete("sys", "user") == "hello"


def test_ollama_provider_passes_json_format_when_requested():
    client = FakeOllamaClient(response={"message": {"content": "{}"}})
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    provider.complete("sys", "user", json_mode=True)
    assert client.last_kwargs["format"] == "json"


def test_ollama_provider_omits_format_when_not_json_mode():
    client = FakeOllamaClient(response={"message": {"content": "hi"}})
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    provider.complete("sys", "user", json_mode=False)
    assert client.last_kwargs["format"] is None


def test_ollama_provider_sends_system_and_user_messages():
    client = FakeOllamaClient(response={"message": {"content": "hi"}})
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    provider.complete("system text", "user text")
    messages = client.last_kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "system text"}
    assert messages[1] == {"role": "user", "content": "user text"}


def test_ollama_provider_wraps_timeout():
    client = FakeOllamaClient(exc=TimeoutError("slow"))
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    with pytest.raises(LLMTimeoutError):
        provider.complete("sys", "user")


def test_ollama_provider_wraps_other_errors():
    client = FakeOllamaClient(exc=ConnectionError("refused"))
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    with pytest.raises(LLMRequestError):
        provider.complete("sys", "user")


def test_ollama_provider_wraps_unexpected_response_shape():
    client = FakeOllamaClient(response={"unexpected": "shape"})
    provider = OllamaProvider(model="llama3.1", host="http://x", client=client)
    with pytest.raises(LLMRequestError):
        provider.complete("sys", "user")


# --- OpenAI / Groq (shared OpenAI-compatible shape) ---


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.response


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeOpenAICompatibleClient:
    def __init__(self, content=None, exc=None):
        self.completions = _FakeCompletions(
            response=_FakeCompletionResponse(content) if content is not None else None,
            exc=exc,
        )
        self.chat = _FakeChat(self.completions)


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, GroqProvider])
def test_openai_compatible_provider_returns_content(provider_cls):
    client = FakeOpenAICompatibleClient(content="hello")
    provider = provider_cls(model="gpt-4o-mini", api_key="fake-key", client=client)
    assert provider.complete("sys", "user") == "hello"


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, GroqProvider])
def test_openai_compatible_provider_requests_json_object_format(provider_cls):
    client = FakeOpenAICompatibleClient(content="{}")
    provider = provider_cls(model="m", api_key="fake-key", client=client)
    provider.complete("sys", "user", json_mode=True)
    assert client.completions.last_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, GroqProvider])
def test_openai_compatible_provider_omits_response_format_when_not_json_mode(provider_cls):
    client = FakeOpenAICompatibleClient(content="hi")
    provider = provider_cls(model="m", api_key="fake-key", client=client)
    provider.complete("sys", "user", json_mode=False)
    assert "response_format" not in client.completions.last_kwargs


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, GroqProvider])
def test_openai_compatible_provider_wraps_timeout(provider_cls):
    client = FakeOpenAICompatibleClient(exc=TimeoutError("slow"))
    provider = provider_cls(model="m", api_key="fake-key", client=client)
    with pytest.raises(LLMTimeoutError):
        provider.complete("sys", "user")


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, GroqProvider])
def test_openai_compatible_provider_wraps_other_errors(provider_cls):
    client = FakeOpenAICompatibleClient(exc=RuntimeError("api error"))
    provider = provider_cls(model="m", api_key="fake-key", client=client)
    with pytest.raises(LLMRequestError):
        provider.complete("sys", "user")


def test_provider_names_are_distinct_and_correct():
    assert OllamaProvider.provider_name == "ollama"
    assert OpenAIProvider.provider_name == "openai"
    assert GroqProvider.provider_name == "groq"
