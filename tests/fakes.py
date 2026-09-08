"""
Test-only fake providers. These implement LLMProvider directly rather
than mocking an SDK, so JobParser tests exercise the real interface
boundary without touching any real network code.
"""

from __future__ import annotations

from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMError


class FakeProvider(LLMProvider):
    """Returns a fixed canned response regardless of the prompt given."""

    provider_name = "fake"

    def __init__(self, model: str, response: str) -> None:
        super().__init__(model)
        self.response = response
        self.last_system: str | None = None
        self.last_prompt: str | None = None
        self.last_json_mode: bool | None = None

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        self.last_system = system
        self.last_prompt = prompt
        self.last_json_mode = json_mode
        return self.response


class FailingProvider(LLMProvider):
    """Always raises an LLMError, simulating a network/timeout failure."""

    provider_name = "fake"

    def __init__(self, model: str, error: LLMError) -> None:
        super().__init__(model)
        self.error = error

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        raise self.error
