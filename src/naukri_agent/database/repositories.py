"""
Repositories: functions that translate between domain models
(CandidateProfile, MasterResume, JobCreate, ...) and ORM rows. Keeping
this separate from database/models.py means the ORM models stay pure
schema, and callers never need to know column names to do common
operations.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.models import Candidate, Job, JobExtraction, JobMatch, ResumeSelection
from naukri_agent.jobs.models import (
    JobCreate,
    JobExtractionCreate,
    compute_content_fingerprint,
    extract_external_id,
)
from naukri_agent.matching.models import MatchResult
from naukri_agent.resume.selector import ResumeSelectionOutcome


def upsert_candidate(session: Session, profile: CandidateProfile) -> Candidate:
    """
    Sync a CandidateProfile into the candidates table, keyed by email.

    This is a single-candidate system for now — in practice there
    will only ever be one row — but keying by email rather than
    assuming id=1 keeps the door open without adding complexity now.
    Call this once at the start of each pipeline run (Phase 11) after
    loading the YAML profile, so JobMatch/Application rows in later
    phases have a stable candidate_id to reference.
    """
    existing = session.query(Candidate).filter_by(email=profile.email).one_or_none()
    profile_json = profile.model_dump_json()

    if existing is None:
        candidate = Candidate(
            full_name=profile.full_name,
            email=profile.email,
            years_experience=profile.years_experience,
            profile_json=profile_json,
        )
        session.add(candidate)
        return candidate

    existing.full_name = profile.full_name
    existing.years_experience = profile.years_experience
    existing.profile_json = profile_json
    existing.synced_at = datetime.datetime.now(datetime.UTC)
    return existing


def upsert_job(
    session: Session, job: JobCreate, run_id: int | None = None
) -> tuple[Job, bool]:
    """
    Insert a newly discovered job, or update the existing row for the
    same listing. Returns (job, created) so callers can distinguish
    genuinely new discoveries from re-sightings — needed for the daily
    report's "Jobs discovered: N" count (Section 18).

    Matching for "is this the same listing":
      1. external_id (parsed from the Naukri URL) if available
      2. exact url match as a fallback

    On a match, raw fields are UPDATED (the live listing may have
    been edited since we last saw it) but first_seen_at never changes
    and times_seen increments — this is a re-sighting, not a new job.

    On no match, a new row is inserted. If its content_fingerprint
    matches an existing job's fingerprint (same title+company+
    description, different URL/external_id), repost_of_job_id is set
    to point at the earliest such job — a likely repost, tracked
    without merging the two listings' independent histories.
    """
    now = datetime.datetime.now(datetime.UTC)
    fingerprint = compute_content_fingerprint(job.title, job.company, job.description)
    external_id = extract_external_id(job.url)

    existing = None
    if external_id:
        existing = session.query(Job).filter_by(external_id=external_id).one_or_none()
    if existing is None:
        existing = session.query(Job).filter_by(url=job.url).one_or_none()

    if existing is not None:
        existing.title = job.title
        existing.company = job.company
        existing.location = job.location
        existing.salary_text = job.salary_text
        existing.experience_text = job.experience_text
        existing.description = job.description
        existing.posted_date_text = job.posted_date_text
        existing.content_fingerprint = fingerprint
        existing.last_seen_at = now
        existing.times_seen += 1
        if run_id is not None:
            existing.last_seen_run_id = run_id
        return existing, False

    repost_of = (
        session.query(Job)
        .filter_by(content_fingerprint=fingerprint)
        .order_by(Job.first_seen_at.asc())
        .first()
    )

    new_job = Job(
        external_id=external_id,
        url=job.url,
        title=job.title,
        company=job.company,
        location=job.location,
        salary_text=job.salary_text,
        experience_text=job.experience_text,
        description=job.description,
        posted_date_text=job.posted_date_text,
        source=job.source,
        content_fingerprint=fingerprint,
        repost_of_job_id=repost_of.id if repost_of is not None else None,
        discovered_at=now,
        first_seen_at=now,
        last_seen_at=now,
        times_seen=1,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
    )
    session.add(new_job)
    session.flush()  # populate new_job.id
    return new_job, True


def add_job_extraction(
    session: Session, job_id: int, extraction: JobExtractionCreate
) -> JobExtraction:
    """
    Persist a new structured extraction for a job WITHOUT touching the
    raw Job row — the LLM-derived fields live only in JobExtraction.
    Previous extractions for the same job are kept (never deleted or
    overwritten) but marked is_current=False, so the full chain stays
    auditable: Job.description (raw) -> JobExtraction.raw_llm_response
    (verbatim LLM output) -> JobExtraction's typed columns (parsed).
    """
    previous_count = session.query(JobExtraction).filter_by(job_id=job_id).count()
    session.query(JobExtraction).filter_by(job_id=job_id, is_current=True).update(
        {"is_current": False}
    )

    row = JobExtraction(
        job_id=job_id,
        extraction_version=previous_count + 1,
        is_current=True,
        llm_provider=extraction.llm_provider,
        llm_model=extraction.llm_model,
        normalized_title=extraction.normalized_title,
        required_skills=extraction.required_skills,
        preferred_skills=extraction.preferred_skills,
        experience_min=extraction.experience_min,
        experience_max=extraction.experience_max,
        salary_min=extraction.salary_min,
        salary_max=extraction.salary_max,
        salary_currency=extraction.salary_currency,
        education_requirements=extraction.education_requirements,
        job_type=extraction.job_type,
        raw_llm_response=extraction.raw_llm_response,
    )
    session.add(row)
    session.flush()
    return row


def upsert_job_match(
    session: Session,
    candidate_id: int,
    job_id: int,
    result: MatchResult,
    job_extraction_id: int | None = None,
) -> tuple[JobMatch, bool]:
    """
    Persist a JobScorer MatchResult for one (candidate, job) pair.
    Returns (job_match, created). A second call for the same pair
    (e.g. after a new JobExtraction version) UPDATES the existing row
    rather than inserting a duplicate — the unique constraint on
    (candidate_id, job_id) makes a duplicate impossible even if this
    function is bypassed.
    """
    existing = (
        session.query(JobMatch)
        .filter_by(candidate_id=candidate_id, job_id=job_id)
        .one_or_none()
    )

    category_scores_json = {
        name: cs.model_dump() for name, cs in result.category_scores.items()
    }

    if existing is not None:
        existing.job_extraction_id = job_extraction_id
        existing.overall_score = result.overall_score
        existing.decision = result.decision
        existing.category_scores = category_scores_json
        existing.positive_factors = result.positive_factors
        existing.negative_factors = result.negative_factors
        existing.matched_at = datetime.datetime.now(datetime.UTC)
        return existing, False

    job_match = JobMatch(
        candidate_id=candidate_id,
        job_id=job_id,
        job_extraction_id=job_extraction_id,
        overall_score=result.overall_score,
        decision=result.decision,
        category_scores=category_scores_json,
        positive_factors=result.positive_factors,
        negative_factors=result.negative_factors,
    )
    session.add(job_match)
    session.flush()
    return job_match, True


def upsert_resume_selection(
    session: Session,
    job_id: int,
    outcome: ResumeSelectionOutcome,
    candidate_id: int | None = None,
) -> tuple[ResumeSelection, bool]:
    """
    Persist a ResumeSelectionOutcome (resume/selector.py) for one
    (candidate, job) pair. Re-selecting for the same pair UPDATES the
    existing row rather than duplicating — selection is idempotent
    (the same job+registry should deterministically produce the same
    outcome), so there's no versioning need here the way there is for
    JobExtraction's LLM-derived content.
    """
    existing = (
        session.query(ResumeSelection)
        .filter_by(candidate_id=candidate_id, job_id=job_id)
        .one_or_none()
    )

    fields = dict(
        resume_id=outcome.resume_id,
        file_path=outcome.file,
        file_hash=outcome.file_hash,
        decision=outcome.decision,
        matched_via=outcome.matched_via,
        reason=outcome.reason,
    )

    if existing is not None:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.selected_at = datetime.datetime.now(datetime.UTC)
        return existing, False

    row = ResumeSelection(job_id=job_id, candidate_id=candidate_id, **fields)
    session.add(row)
    session.flush()
    return row, True


