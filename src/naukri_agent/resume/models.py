"""
MasterResume: canonical, factual career history.

This is the source of truth for resume generation (Phase 6). Nothing
in this codebase is permitted to invent or alter facts here — company
names, dates, and qualifications must only ever come from what you
put in master_resume.yaml yourself (Section 13, rules 1-5).
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class WorkExperience(BaseModel):
    company: str
    title: str
    location: str | None = None
    start_date: datetime.date
    end_date: datetime.date | None = None  # None = current role
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.end_date is None

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "WorkExperience":
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date}) is before start_date "
                f"({self.start_date}) for {self.company} - {self.title}"
            )
        return self


class Education(BaseModel):
    institution: str
    degree: str
    field_of_study: str | None = None
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    grade: str | None = None


class Certification(BaseModel):
    name: str
    issuer: str
    issue_date: datetime.date | None = None
    expiry_date: datetime.date | None = None
    credential_id: str | None = None


class Project(BaseModel):
    name: str
    description: str | None = None
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None


class MasterResume(BaseModel):
    professional_summary: str
    skills: list[str] = Field(default_factory=list)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)

    def content_hash(self) -> str:
        """
        Stable hash of the full resume content. Phase 6's
        ResumeVersion table stores this alongside each generated
        tailored resume so we can later detect that the master resume
        has since changed and a regeneration may be warranted.
        """
        canonical = self.model_dump_json(exclude_none=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def total_years_experience(self) -> float:
        """
        Total years of experience, derived from work_experience date
        ranges rather than stored as a separately-maintained number —
        so it can never drift out of sync with the actual history.
        Overlapping roles are not de-duplicated; each entry counts in
        full, since that's what you'd actually put on a resume.
        """
        today = datetime.date.today()
        total_days = sum(
            max(((job.end_date or today) - job.start_date).days, 0)
            for job in self.work_experience
        )
        return round(total_days / 365.25, 1)


def load_master_resume(path: str | Path) -> MasterResume:
    """
    Load and validate a MasterResume from a YAML file. Raises
    FileNotFoundError with an actionable message if the file doesn't
    exist yet — the real resume is gitignored personal data.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Master resume not found at {path}. Copy "
            "config/master_resume.example.yaml to that path and fill "
            "in your own career history."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return MasterResume.model_validate(data)
