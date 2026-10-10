"""The model client for the researcher: one tool-calling chat turn against Groq.

Kept separate from llm/ on purpose. That layer is a plain text-in, text-out interface that every
provider implements; tool calling is a different contract, and only this agent needs it.
Errors carry the exception TYPE only: provider messages can echo parts of the request.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from naukri_agent.research_agent.models import AgentLLMError, ChatTurn, ToolCall, ToolUseFailed


class GroqChatClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_tokens: int = 1500,
    ) -> None:
        self.model = model
        self._sleep = sleep
        self._max_tokens = max_tokens
        if client is None:
            import groq  # local import: only needed when the agent actually runs

            client = groq.Groq(api_key=api_key, timeout=timeout)
        self._client = client

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: Any = "auto") -> ChatTurn:
        response = None
        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=0,
                    max_tokens=self._max_tokens,
                )
                break
            except Exception as exc:  # noqa: BLE001 - SDK error classes vary
                name = type(exc).__name__
                if "RateLimit" in name and attempt < 2:
                    self._sleep(8 * (attempt + 1))
                    continue
                if "tool_use_failed" in str(exc):
                    raise ToolUseFailed(name) from exc
                raise AgentLLMError(name) from exc
        try:
            message = response.choices[0].message
            calls = [
                ToolCall(id=str(tc.id), name=str(tc.function.name), arguments=tc.function.arguments or "{}")
                for tc in (getattr(message, "tool_calls", None) or [])
            ]
            usage = getattr(response, "usage", None)
            return ChatTurn(
                content=getattr(message, "content", None),
                tool_calls=calls,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            )
        except (AttributeError, IndexError, TypeError) as exc:
            raise AgentLLMError("UnexpectedResponseShape") from exc
