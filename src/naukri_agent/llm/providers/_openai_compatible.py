"""
Shared implementation for providers whose SDK follows the OpenAI
chat-completions shape: client.chat.completions.create(...) ->
response.choices[0].message.content. Both the official `openai` and
`groq` packages follow this shape (Groq's API is OpenAI-compatible),
so the request building, JSON-mode handling, and error wrapping live
here once rather than being duplicated between OpenAIProvider and
GroqProvider — they differ only in which SDK builds the client.
"""

from __future__ import annotations

from typing import Any

from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMRequestError, LLMTimeoutError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, model: str, client: Any) -> None:
        super().__init__(model)
        self._client = client

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                **kwargs,
            )
        except TimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - SDK raises varying exception types
            raise LLMRequestError(str(exc)) from exc

        try:
            return response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise LLMRequestError(f"Unexpected response shape: {response!r}") from exc
