"""What the researcher hands back, and the small types the loop and the model client share."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _cut(value: str, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


class ApplyCandidate(BaseModel):
    url: str
    why: str = ""
    confidence: Literal["high", "medium", "low"] = "low"

    @field_validator("why", mode="before")
    @classmethod
    def _why(cls, v):
        return _cut(v or "", 200)

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v):
        return str(v).strip()[:500]


class ResearchReport(BaseModel):
    """The agent proposes the first five fields; code fills `sources` and `notes` (the model cannot)."""

    company_summary: str
    careers_url: str | None = None
    apply_candidates: list[ApplyCandidate] = Field(default_factory=list)
    lists_this_role: Literal["yes", "no", "unknown"] = "unknown"
    red_flags: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)  # where the company's page disagrees with the Naukri post
    sources: list[str] = Field(default_factory=list)  # pages the agent really fetched
    notes: list[str] = Field(default_factory=list)  # what code changed or could not check

    @field_validator("company_summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return _cut(v or "", 500)

    @field_validator("careers_url", mode="before")
    @classmethod
    def _careers(cls, v):
        return str(v).strip()[:500] if v else None

    @field_validator("red_flags", "differences", mode="before")
    @classmethod
    def _flags(cls, v):
        return [_cut(x, 160) for x in (v or [])][:5]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # the model's raw JSON text


@dataclass
class ChatTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AgentLLMError(RuntimeError):
    """The model call failed. Message is the error TYPE only, never the provider's text."""


class ToolUseFailed(AgentLLMError):
    """The provider rejected the model's tool call as malformed (worth one nudge and retry)."""


@dataclass
class AgentResult:
    report: ResearchReport | None
    error: str | None
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
