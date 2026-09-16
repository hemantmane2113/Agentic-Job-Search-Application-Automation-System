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


# ---------------------------------------------------------------------------
# Skill evidence (2026-09-11 hybrid-skill-source phase, persistence design
# approved 2026-09-11). Two tables, deliberately kept at two different
# lifecycle granularities -- see jobs/skill_evidence.py for the in-memory
# SkillEvidence/MergedSkillEvidence types these mirror.
# ---------------------------------------------------------------------------


class JobRawSkillEvidence(Base):
    """
    Raw, scraped skill evidence for a job's CURRENT fetch — the
    schema.org ld+json `skills` array, or the Key Skills DOM chip
    widget. Nothing here is LLM-derived (see JobExtractionSkillEvidence
    below for that layer).

    job_id is the SPECIFIC fetched Job.id — NEVER canonicalized. A
    repost is fetched independently (its own URL) and gets its own
    row, matching JobExtraction.job_id / RunEvent.job_id, not
    JobRecommendation's canonicalized convention.

    REPLACED wholesale on every re-fetch (see
    database.repositories.replace_raw_skill_evidence) — no historical
    row is kept. This deliberately mirrors Job's OWN raw fields
    (description, salary_text, experience_text, ...), which are
    already overwritten in place on every re-sighting rather than
    versioned; keeping a history here while every other raw field on
    the same Job row is silently overwritten would be an inconsistent,
    one-off exception to how this table's own parent already behaves.
    """

    __tablename__ = "job_raw_skill_evidence"
    __table_args__ = (
        UniqueConstraint("job_id", "source", "skill_text", name="uq_raw_skill_evidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # "ld_json" | "key_skills_dom"
    skill_text: Mapped[str] = mapped_column(String(255), nullable=False)  # raw/original spelling
    # True/False for key_skills_dom (Naukri's own preferred icon);
    # NULL for ld_json (that shape carries no preferred/required
    # signal at all — NULL correctly distinguishes "no signal" from
    # "explicitly not preferred", never guessed as False).
    preferred: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<JobRawSkillEvidence id={self.id} job_id={self.job_id} "
            f"source={self.source!r} skill_text={self.skill_text!r}>"
        )


class JobExtractionSkillEvidence(Base):
    """
    Merged, one-row-per-skill DERIVED skill evidence for ONE SPECIFIC
    JobExtraction version — see jobs/skill_evidence.py's
    MergedSkillEvidence (classification, winning source, the tier-
    based precedence policy, and the never-silently-resolved conflict
    fields all come from there; this table is a straight persistence
    of that type).

    IMMUTABLE once written: a later JobExtraction version gets its OWN
    full snapshot of rows (see
    database.repositories.add_job_extraction_skill_evidence, called
    once per add_job_extraction() call) — never updates or reuses a
    previous version's rows. This is what preserves historical
    reproducibility: an old extraction version's evidence stays fully
    auditable even after a newer fetch has since replaced
    JobRawSkillEvidence's snapshot, and even after a newer extraction
    version exists.

    skill_key is normalize_skill(skill) (matching.skill_normalizer),
    persisted so the unique constraint enforces the SAME skill
    identity the in-memory merge already uses — `skill` itself keeps
    the original/display spelling.
    """

    __tablename__ = "job_extraction_skill_evidence"
    __table_args__ = (
        UniqueConstraint(
            "job_extraction_id", "skill_key", name="uq_extraction_skill_evidence"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_extraction_id: Mapped[int] = mapped_column(
        ForeignKey("job_extractions.id"), nullable=False, index=True
    )
    skill: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(255), nullable=False)
    classification: Mapped[str] = mapped_column(String(15), nullable=False)  # required|preferred|unclassified
    winning_source: Mapped[str] = mapped_column(String(30), nullable=False)
    # list[str] -- every source (classified or not) that mentioned this
    # skill; corroboration/audit only, never a scoring input.
    contributing_sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    classification_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # list[[classification, source], ...] -- every distinct classified
    # claim that LOST to the winner. Empty when there was no conflict.
    conflicting_classifications: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<JobExtractionSkillEvidence id={self.id} "
            f"job_extraction_id={self.job_extraction_id} skill={self.skill!r} "
            f"classification={self.classification!r}>"
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


# ===========================================================================
# Scope change: read-only daily match digest.
#
# Three SEPARATE axes, never conflated:
#   - discovery      : Job / JobExtraction / RunEvent
#   - recommendation : JobRecommendation  (a job appeared in an email)
#   - application    : ApplicationHistory / ApplicationEvent
#                      (the human actually applied — the DB is the ONLY
#                       authority; never inferred, never LLM-set)
# A JobRecommendation row NEVER implies an ApplicationHistory row.
# ===========================================================================


class ApplicationStatus(str, enum.Enum):
    NOT_APPLIED = "NOT_APPLIED"  # explicit "reviewed, not applying"; absence of a row means the same
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"


class RunEventStatus(str, enum.Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplicationHistory(Base):
    """
    The authoritative record of whether the human applied to a job.
    Written ONLY by the manual `mark-applied` / `mark-status` CLI (or a
    future explicit import) — never by discovery, never by the
    recommendation pipeline, never by an LLM. `job_id` is the CANONICAL
    job id (repost root), so a repost of an applied job resolves to the
    same row. Unique on job_id: one application record per underlying
    position; re-marking updates this row (+ an ApplicationEvent),
    never a duplicate.
    """

    __tablename__ = "application_history"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_history_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)

    external_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    job_title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus), default=ApplicationStatus.APPLIED, nullable=False, index=True
    )
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    resume_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resume_file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resume_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(50), default="manual_cli", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    events: Mapped[list["ApplicationEvent"]] = relationship(back_populates="application")

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<ApplicationHistory id={self.id} job_id={self.job_id} "
            f"status={self.status.value}>"
        )


class ApplicationEvent(Base):
    """Audit trail of ApplicationHistory status transitions."""

    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("application_history.id"), nullable=False, index=True
    )
    from_status: Mapped[ApplicationStatus | None] = mapped_column(
        Enum(ApplicationStatus), nullable=True
    )
    to_status: Mapped[ApplicationStatus] = mapped_column(Enum(ApplicationStatus), nullable=False)
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), default="manual_cli", nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    application: Mapped["ApplicationHistory"] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<ApplicationEvent id={self.id} app_id={self.application_id} "
            f"{self.from_status} -> {self.to_status.value}>"
        )


class JobRecommendation(Base):
    """
    Recommendation / email history: one row per (candidate, job, run)
    where the job appeared in the daily digest. Existence here does NOT
    mean the human applied. `job_id` is the CANONICAL job id.
    """

    __tablename__ = "job_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "job_id", "daily_run_id", name="uq_job_recommendation_run"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    job_match_id: Mapped[int | None] = mapped_column(ForeignKey("job_matches.id"), nullable=True)
    daily_run_id: Mapped[int | None] = mapped_column(ForeignKey("daily_runs.id"), nullable=True)

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score_at_email: Mapped[float] = mapped_column(Float, nullable=False)
    decision_at_email: Mapped[MatchDecision] = mapped_column(Enum(MatchDecision), nullable=False)
    application_status_at_email: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus), default=ApplicationStatus.NOT_APPLIED, nullable=False
    )
    resume_id_at_email: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recommended_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False, index=True
    )
    email_status: Mapped[str] = mapped_column(String(20), default="rendered", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<JobRecommendation id={self.id} candidate_id={self.candidate_id} "
            f"job_id={self.job_id} run={self.daily_run_id} rank={self.rank}>"
        )


class RunEvent(Base):
    """Per-stage audit record for one DailyRun. `detail` is SHORT and
    non-sensitive (counts, error class + short message, query name) —
    never credentials, cookies, tokens, PII, raw JD text or LLM output."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    daily_run_id: Mapped[int] = mapped_column(ForeignKey("daily_runs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[RunEventStatus] = mapped_column(Enum(RunEventStatus), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.UTC), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<RunEvent id={self.id} run={self.daily_run_id} {self.stage}={self.status.value}>"

