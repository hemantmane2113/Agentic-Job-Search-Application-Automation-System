from __future__ import annotations

from typing import Any

from naukri_agent.llm.providers._openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    provider_name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        client: Any | None = None,
        timeout: float = 60.0,
    ) -> None:
        client = client if client is not None else self._build_default_client(api_key, timeout)
        super().__init__(model, client)

    @staticmethod
    def _build_default_client(api_key: str, timeout: float) -> Any:
        import openai  # local import: only required if actually used

        return openai.OpenAI(api_key=api_key, timeout=timeout)
