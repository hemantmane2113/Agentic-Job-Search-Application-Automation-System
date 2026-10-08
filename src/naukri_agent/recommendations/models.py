"""View models for the daily recommendation digest (not ORM)."""

from __future__ import annotations

import datetime
import enum

from pydantic import BaseModel, Field

from naukri_agent.database.models import ApplicationStatus, MatchDecision


class FreshnessLabel(str, enum.Enum):
    NEWLY_DISCOVERED = "NEWLY_DISCOVERED"
    SEEN_BEFORE = "SEEN_BEFORE"
    PREVIOUSLY_RECOMMENDED = "PREVIOUSLY_RECOMMENDED"
    PREVIOUSLY_APPLIED = "PREVIOUSLY_APPLIED"


class MatchExplanation(BaseModel):
    """reasons/gaps are the deterministic factor strings; `narrative` is
    optional LLM prose over them. `source` says which."""

    reasons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    narrative: str | None = None
    source: str = "deterministic"  # "deterministic" | "llm"
    narrative_dropped_reason: str | None = None


class Recommendation(BaseModel):
    rank: int
    job_id: int  # canonical Job id
    job_title: str
    company: str
    location: str | None = None
    experience_text: str | None = None
    experience_min: float | None = None
    experience_max: float | None = None
    salary_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None

    match_score: float  # deterministic overall_score (0-100)
    match_decision: MatchDecision

    recommended_resume_id: str | None = None
    recommended_resume_status: str | None = None  # ResumeSelectionDecision value
    recommended_resume_reason: str | None = None

    reasons: list[str] = Field(default_factory=list)  # == MatchResult.positive_factors
    gaps: list[str] = Field(default_factory=list)  # == MatchResult.negative_factors
    explanation: str | None = None  # optional LLM narrative
    explanation_source: str = "deterministic"

    # deterministic factual labels — never LLM-set
    application_status: ApplicationStatus = ApplicationStatus.NOT_APPLIED
    application_status_label: str = "New"
    application_applied_at: datetime.datetime | None = None
    freshness: FreshnessLabel = FreshnessLabel.NEWLY_DISCOVERED
    freshness_label: str = "Newly discovered"

    naukri_url: str  # ALWAYS from Job.url
    # "native" | "company_site" | "none" | None (not yet checked) -- from Job.apply_type
    apply_type: str | None = None


class AppliedViaAgent(BaseModel):
    """A job the app applied to (after the user's Yes on Telegram), for the email's Part 2."""

    job_title: str
    company: str
    location: str | None = None
    job_url: str
    applied_at: datetime.datetime | None = None  # naive UTC, as stored
    source: str = ""
    answers: list[tuple[str, str]] = Field(default_factory=list)  # (question, answer sent)


class RecommendationDigest(BaseModel):
    run_date: datetime.date
    generated_at: datetime.datetime
    candidate_email: str | None = None
    limit: int
    eligible_count: int  # before the cap
    count: int  # after the cap == len(recommendations)
    truncated: bool
    recommendations: list[Recommendation] = Field(default_factory=list)
    run_id: int | None = None
    notes: list[str] = Field(default_factory=list)
    # Two-part email: Part 1 = `recommendations` (apply yourself on the company website),
    # Part 2 = `applied_via_agent`. Off by default so the original single-list layout and
    # its tests are untouched.
    two_part: bool = False
    profile_refresh_note: str | None = None  # what the day's first step (profile refresh) did
    native_waiting: int = 0  # eligible jobs with a Naukri Apply button, left for telegram-apply
    applied_via_agent: list[AppliedViaAgent] = Field(default_factory=list)
