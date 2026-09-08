"""
CandidateProfile: job-search criteria and identity.

This is NOT the canonical career history — see resume/models.py's
MasterResume for that. CandidateProfile is what the matching engine
(Phase 4) scores jobs against, and what application forms (Phase 8)
pull identity fields from. It's meant to be edited often as your
search evolves, which is why it's loaded from a plain YAML file
rather than requiring a code change or migration to update.
"""

from __future__ import annotations

import enum
from pathlib import Path

import yaml
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class WorkMode(str, enum.Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    ANY = "any"


class CandidateProfile(BaseModel):
    full_name: str
    email: EmailStr
    phone: str

    skills: list[str] = Field(default_factory=list)
    years_experience: float = 0

    preferred_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    work_mode: WorkMode = WorkMode.ANY

    expected_salary_min_lpa: float | None = None
    expected_salary_max_lpa: float | None = None
    notice_period_days: int = 0

    industries_of_interest: list[str] = Field(default_factory=list)
    industries_to_avoid: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    keywords_to_avoid: list[str] = Field(default_factory=list)

    @field_validator(
        "skills",
        "preferred_roles",
        "preferred_locations",
        "industries_of_interest",
        "industries_to_avoid",
        "keywords",
        "keywords_to_avoid",
    )
    @classmethod
    def _strip_and_dedupe(cls, values: list[str]) -> list[str]:
        """Trim whitespace and drop duplicates while preserving order."""
        seen: list[str] = []
        for raw in values:
            v = raw.strip()
            if v and v not in seen:
                seen.append(v)
        return seen

    @field_validator("years_experience")
    @classmethod
    def _years_experience_not_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("years_experience cannot be negative")
        return v

    @field_validator("notice_period_days")
    @classmethod
    def _notice_period_not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("notice_period_days cannot be negative")
        return v

    @model_validator(mode="after")
    def _salary_range_is_sane(self) -> "CandidateProfile":
        if (
            self.expected_salary_min_lpa is not None
            and self.expected_salary_max_lpa is not None
            and self.expected_salary_max_lpa < self.expected_salary_min_lpa
        ):
            raise ValueError(
                "expected_salary_max_lpa must be >= expected_salary_min_lpa"
            )
        return self


def load_candidate_profile(path: str | Path) -> CandidateProfile:
    """
    Load and validate a CandidateProfile from a YAML file. Raises
    FileNotFoundError with an actionable message if the file doesn't
    exist yet — this is expected on a fresh checkout, since the real
    profile is gitignored personal data.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Candidate profile not found at {path}. Copy "
            "config/candidate_profile.example.yaml to that path and "
            "fill in your own details."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return CandidateProfile.model_validate(data)
