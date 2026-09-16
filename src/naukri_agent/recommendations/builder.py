"""
build_digest: turn this run's deterministic JobMatch rows into a ranked,
capped, application-aware RecommendationDigest.

Eligibility (deterministic; no LLM):
  A. canonical job has ApplicationHistory.status in
     settings.recommendation_exclude_if_status
     -> EXCLUDE, regardless of recommendation_cooldown_days.
  B. previously RECOMMENDED (JobRecommendation exists) but NOT applied:
       recommendation_cooldown_days == 0 -> eligible again next run.
       recommendation_cooldown_days  > 0 -> eligible only once that many
         days have elapsed since the last recommendation.
  C. never recommended and not applied -> ELIGIBLE.
  D. reposts/duplicates: everything keys on the CANONICAL job id
     (canonical_job_id / resolve_canonical_job), so a repost inherits
     the applied/recommended history of the underlying position. No LLM
     similarity is used anywhere.

Then: rank by overall_score desc (tie-break newest Job.first_seen_at),
cap at settings.daily_recommendation_limit, write one JobRecommendation
row per kept item.
"""

from __future__ import annotations

import datetime
import logging
import re

from sqlalchemy.orm import Session

from naukri_agent.config import Settings
from naukri_agent.database.models import (
    ApplicationStatus,
    Job,
    JobExtraction,
    MatchDecision,
)
from naukri_agent.database.repositories import (
    application_status_for_job,
    canonical_job_id,
    get_application_history,
    latest_job_match,
    latest_recommendation,
    latest_resume_selection,
    record_job_recommendation,
)
from naukri_agent.llm.base import LLMProvider
from naukri_agent.matching.models import CategoryScore, MatchResult
from naukri_agent.orchestration.discovery import _parse_card_age_days
from naukri_agent.recommendations.explain import explain_match
from naukri_agent.recommendations.models import (
    FreshnessLabel,
    Recommendation,
    RecommendationDigest,
)

logger = logging.getLogger(__name__)

_DATE_FMT = "%d %b %Y"


def _naive_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite round-trips datetimes as tz-naive; our `now` is tz-aware.
    Normalise both to naive-UTC before any arithmetic."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.UTC)
    return dt.replace(tzinfo=None)


def _fmt_date(dt: datetime.datetime | None) -> str:
    return dt.strftime(_DATE_FMT) if dt is not None else "unknown date"


def _match_result_from_row(row) -> MatchResult:
    cats = {
        name: CategoryScore(**cs) for name, cs in (row.category_scores or {}).items()
    }
    return MatchResult(
        overall_score=row.overall_score,
        decision=row.decision,
        category_scores=cats,
        positive_factors=list(row.positive_factors or []),
        negative_factors=list(row.negative_factors or []),
    )


def _current_extraction(session: Session, job_id: int) -> JobExtraction | None:
    return (
        session.query(JobExtraction)
        .filter_by(job_id=job_id, is_current=True)
        .one_or_none()
    )


def _min_score(settings: Settings) -> float:
    if settings.recommendation_min_score is not None:
        return float(settings.recommendation_min_score)
    return float(settings.threshold_review)


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


def _posted_age_and_recency(
    raw: str | None, now: datetime.datetime
) -> tuple[int | None, float]:
    """
    Parse ``Job.posted_date_text`` (schema.org ``datePosted`` ISO date/
    datetime, or a relative label like "Posted 3 days ago"/"Just now")
    into ``(age_in_days, recency_ts)``. ``age_in_days`` is None when the
    value is absent or unparseable. ``recency_ts`` is a naive-UTC epoch
    seconds value where LARGER == more recently posted; it is
    ``-inf`` for an unknown date so unknowns always sort last on the
    tie-break. Deterministic — no LLM, no network.
    """
    text = (raw or "").strip()
    if not text:
        return None, float("-inf")
    m = _ISO_DATE_RE.match(text)
    if m:
        token = text[: m.end()]
        try:
            if "T" in token or " " in token:
                dt = datetime.datetime.fromisoformat(token.replace(" ", "T"))
            else:
                dt = datetime.datetime.combine(
                    datetime.date.fromisoformat(token[:10]), datetime.time()
                )
        except ValueError:
            return None, float("-inf")
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.UTC).replace(tzinfo=None)
        now_naive = _naive_utc(now)
        age = max((now_naive - dt).days, 0)
        return age, dt.timestamp()
    age = _parse_card_age_days(text)
    if age is None:
        return None, float("-inf")
    # Synthesize a comparable recency ts from the day-granular relative age
    # so ISO and relative values sort consistently within a bucket.
    return age, _naive_utc(now).timestamp() - age * 86400.0


def _freshness_bucket(age_days: int | None) -> int:
    """0 = posted 0-1d ago, 1 = 2-3d, 2 = 4-7d, 3 = older-than-window or
    unknown posted date. Buckets are the PRIMARY recommendation sort key;
    overall_score DESC orders within a bucket."""
    if age_days is None:
        return 3
    if age_days <= 1:
        return 0
    if age_days <= 3:
        return 1
    if age_days <= 7:
        return 2
    return 3


def _status_label(status: ApplicationStatus, applied_at: datetime.datetime | None) -> str:
    if status == ApplicationStatus.NOT_APPLIED:
        return "New"
    if status == ApplicationStatus.UNKNOWN:
        return "Unknown"
    if status == ApplicationStatus.APPLIED:
        return f"Previously applied — {_fmt_date(applied_at)}"
    pretty = status.value.capitalize()
    return f"{pretty} — {_fmt_date(applied_at)}" if applied_at else pretty


def _freshness(
    session: Session,
    candidate_id: int,
    canon_id: int,
    job: Job,
    app_status: ApplicationStatus,
    app_applied_at: datetime.datetime | None,
    settings: Settings,
    now: datetime.datetime,
) -> tuple[FreshnessLabel, str]:
    if app_status not in (ApplicationStatus.NOT_APPLIED, ApplicationStatus.UNKNOWN):
        return (
            FreshnessLabel.PREVIOUSLY_APPLIED,
            f"Previously applied — {_fmt_date(app_applied_at)}",
        )
    last_rec = latest_recommendation(session, candidate_id, canon_id)
    if last_rec is not None:
        return (
            FreshnessLabel.PREVIOUSLY_RECOMMENDED,
            f"Previously recommended — {_fmt_date(last_rec.recommended_at)}",
        )
    first_seen = _naive_utc(job.first_seen_at)
    age_days = (_naive_utc(now) - first_seen).days if first_seen is not None else 9999
    if age_days <= settings.freshness_new_days:
        return (
            FreshnessLabel.NEWLY_DISCOVERED,
            f"Newly discovered (first seen {_fmt_date(first_seen)})",
        )
    return (
        FreshnessLabel.SEEN_BEFORE,
        f"Seen before (first seen {_fmt_date(first_seen)}, {job.times_seen} sightings)",
    )


def build_digest(
    session: Session,
    *,
    candidate_id: int,
    candidate_email: str | None,
    scored_job_ids: list[int],
    settings: Settings,
    run_id: int | None = None,
    now: datetime.datetime | None = None,
    explain_provider: LLMProvider | None = None,
    parse_failed_job_ids: set[int] | None = None,
) -> RecommendationDigest:
    now = now or datetime.datetime.now(datetime.UTC)
    min_score = _min_score(settings)
    allowed_decisions = {d.upper() for d in settings.recommendation_decisions}
    exclude_statuses = {s.upper() for s in settings.recommendation_exclude_if_status}
    cooldown_days = int(settings.recommendation_cooldown_days)

    # Jobs whose LLM parse was attempted and failed DURING THIS RUN. Their
    # JobMatch was scored on the raw listing only (extraction=None), which
    # can only inflate the score, so they are barred from recommendation
    # eligibility here while keeping their diagnostic JobMatch row. Scoped
    # to this run's set only -- a parse failure in an earlier run does not
    # linger. Collapsed to canonical ids so a repost cannot carry a failed
    # parse through under a different id.
    parse_failed_canonical = {
        canonical_job_id(session, jid) for jid in (parse_failed_job_ids or set())
    }

    # de-dup scored ids by canonical id (reposts collapse)
    canon_seen: set[int] = set()
    candidates: list[tuple[int, Job, MatchResult]] = []
    for jid in scored_job_ids:
        canon_id = canonical_job_id(session, jid)
        if canon_id in canon_seen:
            continue
        canon_seen.add(canon_id)
        job = session.get(Job, canon_id)
        if job is None:
            continue
        row = latest_job_match(session, candidate_id, canon_id) or latest_job_match(
            session, candidate_id, jid
        )
        if row is None:
            continue
        candidates.append((canon_id, job, _match_result_from_row(row)))

    eligible: list[tuple[int, Job, MatchResult]] = []
    for canon_id, job, mr in candidates:
        # LLM parse failed for this job this run -> raw-listing score only;
        # never recommendation-eligible (the JobMatch row still stands for
        # diagnostics / Excel / manual review).
        if canon_id in parse_failed_canonical:
            continue
        if mr.decision.value.upper() not in allowed_decisions:
            continue
        if mr.overall_score < min_score:
            continue

        status = application_status_for_job(session, canon_id)
        # A. applied / downstream status -> exclude regardless of cooldown
        if status.value.upper() in exclude_statuses:
            continue
        # B. previously recommended, not applied -> cooldown gate
        last_rec = latest_recommendation(session, candidate_id, canon_id)
        if last_rec is not None:
            if cooldown_days <= 0:
                pass  # eligible again next run
            else:
                elapsed = (
                    _naive_utc(now) - _naive_utc(last_rec.recommended_at)
                ).total_seconds() / 86400.0
                if elapsed < cooldown_days:
                    continue
        # C. never recommended, not applied -> eligible
        eligible.append((canon_id, job, mr))

    # Bucketed freshness-first ranking (Phase F1): PRIMARY key is the
    # posted-date freshness bucket (0-1d / 2-3d / 4-7d / older-or-unknown),
    # SECONDARY is overall_score DESC within the bucket, then posted
    # recency DESC (posted date/time, NOT first_seen_at), then
    # first_seen_at DESC as a final deterministic tie-break. Eligibility
    # gates above are unchanged; this only orders the survivors.
    def _rank_key(t: tuple[int, Job, MatchResult]) -> tuple[int, float, float, float]:
        _canon, job, mr = t
        age, recency = _posted_age_and_recency(job.posted_date_text, now)
        first_seen_ts = job.first_seen_at.timestamp() if job.first_seen_at else 0.0
        return (_freshness_bucket(age), -mr.overall_score, -recency, -first_seen_ts)

    eligible.sort(key=_rank_key)
    eligible_count = len(eligible)
    kept = eligible[: settings.daily_recommendation_limit]

    recs: list[Recommendation] = []
    for rank, (canon_id, job, mr) in enumerate(kept, start=1):
        extraction = _current_extraction(session, canon_id)
        sel = latest_resume_selection(session, canon_id)
        app = get_application_history(session, canon_id)
        app_status = app.status if app else ApplicationStatus.NOT_APPLIED
        app_applied_at = app.applied_at if app else None
        freshness, freshness_label = _freshness(
            session, candidate_id, canon_id, job, app_status, app_applied_at, settings, now
        )
        expl = explain_match(
            mr,
            job_title=job.title,
            company=job.company,
            application_status_label=_status_label(app_status, app_applied_at),
            freshness_label=freshness_label,
            provider=explain_provider,
        )
        rec = Recommendation(
            rank=rank,
            job_id=canon_id,
            job_title=job.title,
            company=job.company,
            location=job.location,
            experience_text=job.experience_text,
            experience_min=extraction.experience_min if extraction else None,
            experience_max=extraction.experience_max if extraction else None,
            salary_text=job.salary_text,
            salary_min=extraction.salary_min if extraction else None,
            salary_max=extraction.salary_max if extraction else None,
            salary_currency=extraction.salary_currency if extraction else None,
            match_score=mr.overall_score,
            match_decision=mr.decision,
            recommended_resume_id=sel.resume_id if sel else None,
            recommended_resume_status=sel.decision.value if sel else None,
            recommended_resume_reason=sel.reason if sel else None,
            reasons=expl.reasons,
            gaps=expl.gaps,
            explanation=expl.narrative,
            explanation_source=expl.source,
            application_status=app_status,
            application_status_label=_status_label(app_status, app_applied_at),
            application_applied_at=app_applied_at,
            freshness=freshness,
            freshness_label=freshness_label,
            naukri_url=job.url,  # ALWAYS from the DB
        )
        recs.append(rec)
        row = latest_job_match(session, candidate_id, canon_id)
        record_job_recommendation(
            session,
            candidate_id=candidate_id,
            job_id=canon_id,
            daily_run_id=run_id,
            rank=rank,
            score_at_email=mr.overall_score,
            decision_at_email=mr.decision,
            application_status_at_email=app_status,
            job_match_id=row.id if row else None,
            resume_id_at_email=sel.resume_id if sel else None,
            recommended_at=now,
            email_status="rendered",
        )

    return RecommendationDigest(
        run_date=now.date(),
        generated_at=now,
        candidate_email=candidate_email,
        limit=settings.daily_recommendation_limit,
        eligible_count=eligible_count,
        count=len(recs),
        truncated=eligible_count > settings.daily_recommendation_limit,
        recommendations=recs,
        run_id=run_id,
    )
