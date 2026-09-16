"""Shared fakes/helpers for the daily-match-digest (Stage A) tests."""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from naukri_agent.config import Settings
from naukri_agent.database.base import make_session_factory
from naukri_agent.database.models import Base, Candidate, DailyRun, DailyRunStatus
from naukri_agent.database.repositories import upsert_job, upsert_job_match, upsert_resume_selection
from naukri_agent.jobs.models import JobCreate
from naukri_agent.matching.models import CategoryScore, MatchDecision, MatchResult
from naukri_agent.resume.registry import (
    ResumeMatchVia,
    ResumeSelectionDecision,
)
from naukri_agent.resume.selector import ResumeSelectionOutcome

from sqlalchemy import create_engine

NAUKRI = "https://www.naukri.com/job-listings-{slug}-{ext}"


def settings(tmp_path, **over) -> Settings:
    base = dict(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'digest.db'}",
        candidate_profile_path=str(tmp_path / "cand.yaml"),
        master_resume_path=str(tmp_path / "resume.yaml"),
        resume_registry_path=str(tmp_path / "resumes.yaml"),
        email_output_dir=str(tmp_path / "emails"),
        excel_path=str(tmp_path / "history.xlsx"),
        dry_run=True,
        daily_recommendation_limit=10,
        recommendation_cooldown_days=30,
        threshold_review=50,
        threshold_accept=80,
    )
    base.update(over)
    return Settings(**base)


def in_memory_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_candidate(session: Session, email: str = "c@example.com") -> Candidate:
    c = Candidate(full_name="Test C", email=email, years_experience=4, profile_json="{}")
    session.add(c)
    session.flush()
    return c


def make_run(session: Session, started_at: datetime.datetime | None = None) -> DailyRun:
    run = DailyRun(
        status=DailyRunStatus.STARTED,
        started_at=started_at or datetime.datetime.now(datetime.UTC),
    )
    session.add(run)
    session.flush()
    return run


def add_job(
    session: Session,
    *,
    slug: str,
    ext: str,
    title: str = "Data Scientist",
    company: str = "Acme",
    location: str = "Pune",
    description: str | None = None,
    first_seen_days_ago: int = 0,
    salary_text: str | None = "10-15 LPA",
    experience_text: str | None = "2-5 yrs",
    posted_date_text: str | None = None,
):
    url = NAUKRI.format(slug=slug, ext=ext)
    job, _ = upsert_job(
        session,
        JobCreate(
            title=title, company=company, location=location,
            description=description if description is not None else f"JD for {slug}.",
            url=url,
            salary_text=salary_text, experience_text=experience_text,
            posted_date_text=posted_date_text,
        ),
    )
    if first_seen_days_ago:
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=first_seen_days_ago)
        job.first_seen_at = old
    session.flush()
    return job


def score(
    session: Session,
    candidate_id: int,
    job_id: int,
    *,
    overall: float,
    decision: MatchDecision = MatchDecision.ACCEPT,
    positives=("skills match: Python, SQL",),
    negatives=("salary below expectation",),
):
    mr = MatchResult(
        overall_score=overall,
        decision=decision,
        category_scores={"skills": CategoryScore(points=overall, max_points=100)},
        positive_factors=list(positives),
        negative_factors=list(negatives),
    )
    upsert_job_match(session, candidate_id, job_id, mr)
    return mr


def select_static_resume(session: Session, job_id: int, candidate_id: int, resume_id="data_scientist"):
    outcome = ResumeSelectionOutcome(
        decision=ResumeSelectionDecision.SELECTED,
        resume_id=resume_id,
        file=f"resumes/{resume_id}.pdf",
        file_hash="deadbeef",
        matched_via=ResumeMatchVia.DETERMINISTIC,
        reason="role match",
    )
    upsert_resume_selection(session, job_id, outcome, candidate_id=candidate_id)


class FakeLLMProvider:
    """Returns a canned narrative string. provider_name/model for audit."""

    provider_name = "fake"
    model = "fake-1"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        return self._text
