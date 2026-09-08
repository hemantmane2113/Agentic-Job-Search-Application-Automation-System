"""
LLMProvider: the interface every concrete provider implements, and
the ONLY thing agent/parser code (JobParser, and later ResumeAgent)
is allowed to depend on. No caller should ever import openai, groq,
or ollama directly — see llm/factory.py for how a concrete provider
gets constructed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    provider_name: str  # set by each concrete subclass, e.g. "ollama"

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        """
        Send a system+user prompt pair and return the model's raw
        text response.

        json_mode is a best-effort request (handled per-provider,
        per Section 7's requirement that capability differences stay
        in the provider layer) that the response be a JSON object.
        Callers must still validate the returned text — no provider
        is trusted to guarantee valid JSON just because json_mode
        was requested.

        Raises an naukri_agent.llm.exceptions.LLMError subclass on
        any failure (network, timeout, auth, malformed SDK response).
        Never raises an unrelated exception type and never returns
        None.
        """
