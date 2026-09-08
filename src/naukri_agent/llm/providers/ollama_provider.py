"""
OllamaProvider: talks to a local Ollama runtime.

The real `ollama` SDK is imported lazily (only when actually
constructing a default client) so importing this module never
requires the package to be installed unless you're actually using it
— tests inject a fake client instead and never touch the real SDK.
"""

from __future__ import annotations

from typing import Any

from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMRequestError, LLMTimeoutError


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

    def __init__(
        self,
        model: str,
        host: str,
        client: Any | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(model)
        self.host = host
        self._client = client if client is not None else self._build_default_client(host, timeout)

    @staticmethod
    def _build_default_client(host: str, timeout: float) -> Any:
        import ollama  # local import: only required if actually used

        return ollama.Client(host=host, timeout=timeout)

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        try:
            response = self._client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                format="json" if json_mode else None,
            )
        except TimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - SDK raises varying exception types
            raise LLMRequestError(str(exc)) from exc

        try:
            return response["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMRequestError(f"Unexpected Ollama response shape: {response!r}") from exc
