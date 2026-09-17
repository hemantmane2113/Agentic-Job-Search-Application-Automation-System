"""
Repositories: functions that translate between domain models
(CandidateProfile, MasterResume, JobCreate, ...) and ORM rows. Keeping
this separate from database/models.py means the ORM models stay pure
schema, and callers never need to know column names to do common
operations.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.models import (
    ApplicationEvent,
    ApplicationHistory,
    ApplicationStatus,
    Candidate,
    Job,
    JobExtraction,
    JobExtractionSkillEvidence,
    JobMatch,
    JobRawSkillEvidence,
    JobRecommendation,
    ResumeSelection,
    RunEvent,
    RunEventStatus,
)
from naukri_agent.jobs.models import (
    JobCreate,
    JobExtractionCreate,
    compute_content_fingerprint,
    extract_external_id,
)
from naukri_agent.matching.models import MatchDecision, MatchResult
from naukri_agent.matching.skill_normalizer import normalize_skill
from naukri_agent.resume.selector import ResumeSelectionOutcome

logger = logging.getLogger(__name__)


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

    On a re-sighting, _repair_stale_repost_links() additionally BREAKS
    (never creates) any repost link whose justifying condition —
    identical content — no longer holds now that a later fetch has
    proven the content distinct. See that helper.
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
        repaired = _repair_stale_repost_links(session, existing)
        if repaired:
            logger.info(
                "upsert_job: cleared stale repost link on job id(s) %s "
                "(content diverged from the linked original; url=%s)",
                repaired,
                job.url,
            )
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


def _repair_stale_repost_links(session: Session, job: Job) -> list[int]:
    """
    Break repost links that a later fetch has proven wrong.

    A repost link (Job.repost_of_job_id) means "same content, different
    URL/external_id". It is created once, at INSERT time, from an exact
    content_fingerprint match. When the FIRST fetch of two listings
    returned empty/identical content they were wrongly linked; a later
    fetch with real, distinct content refreshes each fingerprint but not
    the historical link, so canonical_job_id() keeps collapsing them.

    This runs on every re-sighting, AFTER `job.content_fingerprint` has
    been refreshed, and clears a link only when its justifying condition
    — exact fingerprint equality — no longer holds:

      (a) `job` itself: if it reposts a parent whose current fingerprint
          differs from `job`'s (or the parent is gone), clear
          `job.repost_of_job_id`.
      (b) `job`'s direct children: any row that reposts `job` but whose
          fingerprint now differs from `job`'s is de-linked.

    It NEVER creates a link, NEVER merges rows, NEVER fuzzy-matches, and
    NEVER rewrites application/recommendation/match rows — those keep the
    job_id they were written with. Genuine reposts (content still
    byte-identical after normalisation) are left untouched. Deeper stale
    chains heal incrementally as each link's row is re-sighted.

    Returns the ids of rows whose repost_of_job_id was cleared.
    """
    repaired: list[int] = []

    if job.repost_of_job_id is not None:
        parent = session.get(Job, job.repost_of_job_id)
        if parent is None or parent.content_fingerprint != job.content_fingerprint:
            job.repost_of_job_id = None
            repaired.append(job.id)

    children = session.query(Job).filter(Job.repost_of_job_id == job.id).all()
    for child in children:
        if child.content_fingerprint != job.content_fingerprint:
            child.repost_of_job_id = None
            repaired.append(child.id)

    return repaired


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


def replace_raw_skill_evidence(
    session: Session,
    job_id: int,
    *,
    ld_json_skills: list[str] | None = None,
    key_skills_dom: "list | None" = None,
) -> list[JobRawSkillEvidence]:
    """
    Replace the CURRENT raw skill-evidence snapshot for one job.
    Deletes any existing job_raw_skill_evidence rows for this job_id
    FIRST, then inserts fresh rows for whatever was passed — never
    accumulates historical rows across fetches, mirroring how Job's
    own raw fields (description, salary_text, ...) are already
    overwritten in place on every re-sighting (see upsert_job).

    job_id must be the SPECIFIC fetched Job.id, never canonicalized —
    same convention as add_job_extraction/add_run_event.

    Defensively de-duplicates (source, skill_text) pairs within the
    call so a messy input (e.g. a duplicate chip) can never violate
    the (job_id, source, skill_text) unique constraint. Accepts
    anything with `.text`/`.preferred` attributes for key_skills_dom
    entries (browser.models.KeySkillChip in production; duck-typed in
    tests) so this stays decoupled from browser/.

    Caller-transactional: uses the caller's existing session, never
    commits — the delete-then-insert only becomes durable (or is fully
    undone) when the caller's own session_scope commits or rolls back.
    """
    session.query(JobRawSkillEvidence).filter_by(job_id=job_id).delete()

    seen: set[tuple[str, str]] = set()
    rows: list[JobRawSkillEvidence] = []

    def _add(source: str, text: str, preferred: bool | None) -> None:
        key = (source, text)
        if key in seen:
            return
        seen.add(key)
        row = JobRawSkillEvidence(
            job_id=job_id, source=source, skill_text=text, preferred=preferred
        )
        session.add(row)
        rows.append(row)

    for skill in ld_json_skills or []:
        if skill and skill.strip():
            _add("ld_json", skill, None)
    for chip in key_skills_dom or []:
        text = getattr(chip, "text", None)
        if text and text.strip():
            _add("key_skills_dom", text, bool(getattr(chip, "preferred", False)))

    session.flush()
    return rows


def add_job_extraction_skill_evidence(
    session: Session,
    job_extraction_id: int,
    merged_evidence: "list",  # list[jobs.skill_evidence.MergedSkillEvidence]
) -> list[JobExtractionSkillEvidence]:
    """
    Persist ONE immutable snapshot of merged skill evidence for a
    SPECIFIC JobExtraction version — call this once, right alongside
    add_job_extraction(), never to update or reuse another version's
    rows. A later re-extraction creates its own new JobExtraction row
    (add_job_extraction's existing behaviour) and its own new,
    completely separate set of evidence rows here — the previous
    version's rows are never touched, which is what keeps an old
    extraction fully auditable after a later fetch or re-extraction.

    Defensively de-duplicates by normalize_skill(skill) (merge_skill_
    evidence already guarantees this upstream, but this stays safe
    even if called with a raw, non-deduped list).
    """
    seen: set[str] = set()
    rows: list[JobExtractionSkillEvidence] = []
    for m in merged_evidence:
        key = normalize_skill(m.skill)
        if key in seen:
            continue
        seen.add(key)
        row = JobExtractionSkillEvidence(
            job_extraction_id=job_extraction_id,
            skill=m.skill,
            skill_key=key,
            classification=m.classification,
            winning_source=m.source,
            contributing_sources=list(m.contributing_sources),
            classification_conflict=m.classification_conflict,
            conflicting_classifications=[list(pair) for pair in m.conflicting_classifications],
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


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


# ---------------------------------------------------------------------------
# Scope change: job identity, application history, recommendation history,
# run audit. The DB is the source of truth for application status; nothing
# here consults an LLM.
# ---------------------------------------------------------------------------


def canonical_job_id(session: Session, job_id: int) -> int:
    """Walk Job.repost_of_job_id to the earliest (root) job so a repost
    of a position resolves to one canonical id everywhere."""
    seen: set[int] = set()
    current = job_id
    while current not in seen:
        seen.add(current)
        job = session.get(Job, current)
        if job is None or job.repost_of_job_id is None:
            return current
        current = job.repost_of_job_id
    return current


def resolve_canonical_job(session: Session, ident: "int | str") -> Job | None:
    """
    Resolve a job by internal id, Naukri external id, or exact URL —
    then to its canonical (repost-root) Job. NEVER matches on fuzzy
    title/company and NEVER uses LLM similarity. Returns None (not a
    guess) if nothing matches.
    """
    job: Job | None = None
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit() and "//" not in ident):
        job = session.get(Job, int(ident))
    if job is None and isinstance(ident, str):
        ext = extract_external_id(ident) or (ident if ident.isdigit() else None)
        if ext:
            job = session.query(Job).filter_by(external_id=ext).one_or_none()
    if job is None and isinstance(ident, str):
        job = session.query(Job).filter_by(url=ident).one_or_none()
    if job is None:
        return None
    return session.get(Job, canonical_job_id(session, job.id))


def application_status_for_job(session: Session, job_id: int) -> ApplicationStatus:
    """Status of the CANONICAL job. Absence of a row == NOT_APPLIED."""
    canon = canonical_job_id(session, job_id)
    row = session.query(ApplicationHistory).filter_by(job_id=canon).one_or_none()
    return row.status if row is not None else ApplicationStatus.NOT_APPLIED


def get_application_history(session: Session, job_id: int) -> ApplicationHistory | None:
    return (
        session.query(ApplicationHistory)
        .filter_by(job_id=canonical_job_id(session, job_id))
        .one_or_none()
    )


def upsert_application_history(
    session: Session,
    job_id: int,
    *,
    status: ApplicationStatus = ApplicationStatus.APPLIED,
    applied_at: "datetime.datetime | None" = None,
    resume_id: str | None = None,
    resume_file_path: str | None = None,
    resume_file_hash: str | None = None,
    source: str = "manual_cli",
    note: str | None = None,
) -> tuple[ApplicationHistory, bool]:
    """
    Create or update the ONE ApplicationHistory row for a job's
    canonical id, and record an ApplicationEvent for the transition.
    Idempotent: re-marking never inserts a duplicate. This is the only
    write path for application status besides set_application_status().
    """
    canon = canonical_job_id(session, job_id)
    job = session.get(Job, canon)
    now = datetime.datetime.now(datetime.UTC)
    existing = session.query(ApplicationHistory).filter_by(job_id=canon).one_or_none()

    if existing is None:
        row = ApplicationHistory(
            job_id=canon,
            external_job_id=(job.external_id if job else None),
            job_url=(job.url if job else str(job_id)),
            job_title=(job.title if job else ""),
            company=(job.company if job else ""),
            location=(job.location if job else None),
            status=status,
            applied_at=applied_at
            or (now if status in (ApplicationStatus.APPLIED,) else None),
            resume_id=resume_id,
            resume_file_path=resume_file_path,
            resume_file_hash=resume_file_hash,
            source=source,
            notes=note,
        )
        session.add(row)
        session.flush()
        session.add(
            ApplicationEvent(
                application_id=row.id,
                from_status=None,
                to_status=status,
                source=source,
                note=note,
            )
        )
        session.flush()
        return row, True

    from_status = existing.status
    existing.status = status
    if applied_at is not None:
        existing.applied_at = applied_at
    elif status == ApplicationStatus.APPLIED and existing.applied_at is None:
        existing.applied_at = now
    if resume_id is not None:
        existing.resume_id = resume_id
        existing.resume_file_path = resume_file_path
        existing.resume_file_hash = resume_file_hash
    if note is not None:
        existing.notes = note
    existing.source = source
    existing.updated_at = now
    session.add(
        ApplicationEvent(
            application_id=existing.id,
            from_status=from_status,
            to_status=status,
            source=source,
            note=note,
        )
    )
    session.flush()
    return existing, False


def set_application_status(
    session: Session,
    job_id: int,
    new_status: ApplicationStatus,
    *,
    source: str = "manual_cli",
    note: str | None = None,
) -> ApplicationHistory:
    row, _created = upsert_application_history(
        session, job_id, status=new_status, source=source, note=note
    )
    return row


def excluded_job_ids_by_status(
    session: Session, statuses: "list[str] | set[str]"
) -> set[int]:
    """Canonical job ids whose ApplicationHistory.status is in `statuses`
    — excluded from recommendations regardless of cooldown."""
    wanted = {s.upper() for s in statuses}
    if not wanted:
        return set()
    rows = (
        session.query(ApplicationHistory.job_id)
        .filter(ApplicationHistory.status.in_([ApplicationStatus(s) for s in wanted if s in ApplicationStatus.__members__]))
        .all()
    )
    return {r[0] for r in rows}


def list_applications(
    session: Session, *, status: ApplicationStatus | None = None
) -> list[ApplicationHistory]:
    q = session.query(ApplicationHistory)
    if status is not None:
        q = q.filter_by(status=status)
    return q.order_by(ApplicationHistory.updated_at.desc()).all()


def latest_job_match(session: Session, candidate_id: int, job_id: int) -> JobMatch | None:
    return (
        session.query(JobMatch)
        .filter_by(candidate_id=candidate_id, job_id=job_id)
        .one_or_none()
    )


def latest_resume_selection(session: Session, job_id: int) -> ResumeSelection | None:
    return (
        session.query(ResumeSelection)
        .filter_by(job_id=job_id)
        .order_by(ResumeSelection.selected_at.desc())
        .first()
    )


def job_recommendation_history(
    session: Session, candidate_id: int, job_id: int
) -> list[JobRecommendation]:
    canon = canonical_job_id(session, job_id)
    return (
        session.query(JobRecommendation)
        .filter_by(candidate_id=candidate_id, job_id=canon)
        .order_by(JobRecommendation.recommended_at.asc())
        .all()
    )


def latest_recommendation(
    session: Session, candidate_id: int, job_id: int
) -> JobRecommendation | None:
    history = job_recommendation_history(session, candidate_id, job_id)
    return history[-1] if history else None


def record_job_recommendation(
    session: Session,
    *,
    candidate_id: int,
    job_id: int,
    daily_run_id: int | None,
    rank: int,
    score_at_email: float,
    decision_at_email: MatchDecision,
    application_status_at_email: ApplicationStatus,
    job_match_id: int | None = None,
    resume_id_at_email: str | None = None,
    recommended_at: "datetime.datetime | None" = None,
    email_status: str = "rendered",
) -> JobRecommendation:
    canon = canonical_job_id(session, job_id)
    row = JobRecommendation(
        candidate_id=candidate_id,
        job_id=canon,
        job_match_id=job_match_id,
        daily_run_id=daily_run_id,
        rank=rank,
        score_at_email=score_at_email,
        decision_at_email=decision_at_email,
        application_status_at_email=application_status_at_email,
        resume_id_at_email=resume_id_at_email,
        recommended_at=recommended_at or datetime.datetime.now(datetime.UTC),
        email_status=email_status,
    )
    session.add(row)
    session.flush()
    return row


def add_run_event(
    session: Session,
    *,
    daily_run_id: int,
    seq: int,
    stage: str,
    status: RunEventStatus,
    job_id: int | None = None,
    detail: dict | None = None,
) -> RunEvent:
    row = RunEvent(
        daily_run_id=daily_run_id,
        seq=seq,
        stage=stage,
        status=status,
        job_id=job_id,
        detail=detail,
    )
    session.add(row)
    session.flush()
    return row


