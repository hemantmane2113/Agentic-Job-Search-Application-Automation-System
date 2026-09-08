"""
ORM models.

Phase 1 defined DailyRun to prove the database layer end to end.
Phase 2 added Candidate — a thin relational anchor for the
CandidateProfile whose canonical content lives in YAML.
Phase 3 adds Job (RAW scraped data) and JobExtraction (DERIVED/LLM
data) as two separate tables — see jobs/models.py for the rationale.
Phase 4 adds JobMatch. Phase 6 adds ResumeSelection — recording which
of the candidate's existing, manually-created resume files was
selected for a job (see resume/registry.py and resume/selector.py;
there is no resume-GENERATION table, since nothing in this system
generates resume content). The remaining tables described in the
master spec (Application, ApplicationQuestion, ApplicationEvent) are
added in Phase 8-9.
"""

from __future__ import annotations

import datetime
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from naukri_agent.database.base import Base
from naukri_agent.jobs.models import JobType
from naukri_agent.matching.models import MatchDecision
from naukri_agent.resume.registry import ResumeMatchVia, ResumeSelectionDecision


class DailyRunStatus(str, enum.Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DailyRun(Base):
    """
    One row per execution of the daily pipeline (Phase 17). Tracks
    when it ran, how it ended, and top-line counts for the summary
    email (Phase 18). Later phases will add a foreign key from
    Application -> DailyRun.
    """

    __tablename__ = "daily_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    status: Mapped[DailyRunStatus] = mapped_column(
        Enum(DailyRunStatus), default=DailyRunStatus.STARTED, nullable=False
    )
    jobs_discovered: Mapped[int] = mapped_column(Integer, default=0)
    jobs_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    applications_submitted: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<DailyRun id={self.id} status={self.status.value}>"


class Candidate(Base):
    """
    Relational anchor for the candidate's identity, used as a foreign
    key target by later phases (JobMatch.candidate_id,
    Application.candidate_id). This table is a SYNCED SNAPSHOT, not
    the source of truth — the real CandidateProfile content lives in
    candidate_profile.yaml and is loaded via
    candidate.models.load_candidate_profile(). A few frequently-
    queried scalar fields are duplicated as real columns for
    convenience; the full profile is also kept as a JSON snapshot
    (profile_json) so nothing is lost between syncs without needing a
    separate column per CandidateProfile field.
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    years_experience: Mapped[float] = mapped_column(Float, default=0)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<Candidate id={self.id} email={self.email!r}>"


class Job(Base):
    """
    RAW job data as scraped from Naukri (or another source). Nothing
    in this table is ever written by an LLM — only by the scraper
    (Phase 7) that discovered/re-discovered the listing. See
    JobExtraction below for the separate, LLM-derived layer.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity / dedup
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repost_of_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id"), nullable=True
    )

    # Raw listing content
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    salary_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    experience_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    posted_date_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="naukri")

    # Sighting bookkeeping
    discovered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    times_seen: Mapped[int] = mapped_column(Integer, default=1)

    # Which daily pipeline run(s) touched this job — supports the
    # "Jobs discovered: 84" style daily report (Section 18) being
    # computed from real data rather than a separately-tracked count.
    first_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("daily_runs.id"), nullable=True
    )
    last_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("daily_runs.id"), nullable=True
    )

    extractions: Mapped[list["JobExtraction"]] = relationship(back_populates="job")

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<Job id={self.id} title={self.title!r} company={self.company!r}>"


class JobExtraction(Base):
    """
    DERIVED/structured job data, produced by an LLM parser (Phase 5).
    Every row here is additive: a re-extraction never updates a
    previous row, it inserts a new one and flips is_current on the
    old one to False, so the full extraction history — including the
    raw LLM output that produced each version — stays auditable.
    """

    __tablename__ = "job_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    extraction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extracted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    # Which provider/model produced this extraction — audit trail for
    # the LLM abstraction described in Phase 5's design.
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    normalized_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    required_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preferred_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    experience_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    education_requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    job_type: Mapped[JobType | None] = mapped_column(Enum(JobType), nullable=True)

    # The LLM's actual output, verbatim, before parsing into the
    # typed columns above. This is what makes the extraction step
    # auditable rather than a black box.
    raw_llm_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="extractions")

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<JobExtraction id={self.id} job_id={self.job_id} "
            f"version={self.extraction_version} current={self.is_current}>"
        )


class JobMatch(Base):
    """
    The output of the deterministic JobScorer (Phase 4) for one
    (candidate, job) pair. job_extraction_id records exactly which
    extraction version produced this score, continuing the audit
    chain: Job.description -> JobExtraction -> JobMatch -> decision.

    UniqueConstraint on (candidate_id, job_id) prevents duplicate
    matches — re-scoring the same pair updates this row (see
    database.repositories.upsert_job_match) rather than inserting a
    second one.
    """

    __tablename__ = "job_matches"
    __table_args__ = (UniqueConstraint("candidate_id", "job_id", name="uq_candidate_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    job_extraction_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_extractions.id"), nullable=True
    )

    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    decision: Mapped[MatchDecision] = mapped_column(Enum(MatchDecision), nullable=False)

    # dict[str, {"points": float, "max_points": float,
    #            "positive_factors": [...], "negative_factors": [...]}]
    category_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    positive_factors: Mapped[list] = mapped_column(JSON, nullable=False)
    negative_factors: Mapped[list] = mapped_column(JSON, nullable=False)

    matched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    candidate: Mapped["Candidate"] = relationship()
    job: Mapped["Job"] = relationship()
    job_extraction: Mapped["JobExtraction | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<JobMatch id={self.id} candidate_id={self.candidate_id} "
            f"job_id={self.job_id} score={self.overall_score} "
            f"decision={self.decision.value}>"
        )


class ResumeSelection(Base):
    """
    Which of the candidate's existing, manually-created resume files
    (see resume/registry.py) was selected for a job, and how. There
    is no resume-generation or -versioning here — the file itself
    never changes; this table just records the selection decision so
    a future ApplicationAgent (Phase 8) knows which file to use, and
    so the decision is auditable after the fact.

    Unique on (candidate_id, job_id): re-running selection for the
    same pair UPDATES this row (see
    database.repositories.upsert_resume_selection) rather than
    inserting a duplicate — same pattern as JobMatch.
    """

    __tablename__ = "resume_selections"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_resume_selection_candidate_job"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)

    resume_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    decision: Mapped[ResumeSelectionDecision] = mapped_column(
        Enum(ResumeSelectionDecision), nullable=False
    )
    matched_via: Mapped[ResumeMatchVia | None] = mapped_column(Enum(ResumeMatchVia), nullable=True)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)

    selected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<ResumeSelection id={self.id} job_id={self.job_id} "
            f"resume_id={self.resume_id!r} decision={self.decision.value}>"
        )

