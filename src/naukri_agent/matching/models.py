"""
Shared types for the matching engine. Every sub-scorer
(skill_matcher, experience_matcher, ...) returns a CategoryScore;
JobScorer combines them into a MatchResult.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class MatchDecision(str, enum.Enum):
    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class CategoryScore(BaseModel):
    """Points earned out of this category's configured max, plus why."""

    points: float
    max_points: float
    positive_factors: list[str] = Field(default_factory=list)
    negative_factors: list[str] = Field(default_factory=list)

    @property
    def ratio(self) -> float:
        if self.max_points == 0:
            return 1.0
        return self.points / self.max_points


class MatchResult(BaseModel):
    """
    The full, explainable output of JobScorer.score(). Everything
    needed to reconstruct "why did this job get this score" lives
    here — nothing is discarded before it reaches the caller.
    """

    overall_score: float  # 0-100
    decision: MatchDecision
    category_scores: dict[str, CategoryScore]
    positive_factors: list[str] = Field(default_factory=list)
    negative_factors: list[str] = Field(default_factory=list)


class ExperienceProfile(BaseModel):
    """
    Total vs relevant/skill-specific experience, kept as distinct
    fields rather than a single number. total_years drives the
    Experience category score (compared against a job's stated
    experience_min/max). skill_years is a best-effort, auditable
    lower bound derived from MasterResume work history — used only
    for explanatory context in Phase 4, since JobExtraction does not
    yet carry per-skill requirements to score it against. See
    experience_matcher.derive_skill_years_from_resume for how it's
    computed and its limitations.
    """

    total_years: float
    skill_years: dict[str, float] = Field(default_factory=dict)
