"""
Read-only Naukri job discovery for the daily digest.

Builds a query matrix from the CandidateProfile, runs the existing
search, fetches each job's public detail page, and upserts a raw Job
row (dedup via the existing external-id / URL / fingerprint / repost
primitives). Never clicks Apply, fills a form, or touches the
application workflow.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel
from sqlalchemy.orm import Session

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import RunEventStatus
from naukri_agent.database.repositories import add_run_event, replace_raw_skill_evidence, upsert_job
from naukri_agent.jobs.models import JobCreate

logger = logging.getLogger(__name__)


class DiscoveryQuery(BaseModel):
    role: str
    location: str = ""


class DiscoveryResult(BaseModel):
    queries_run: int
    queries_failed: int
    jobs_new: int
    jobs_reseen: int
    details_failed: int
    job_ids: list[int]  # every Job row touched this run
    total_failure: bool  # every query failed and nothing was stored


_AGE_ZERO_MARKERS = (
    "just now",
    "few hours ago",
    "few minutes ago",
    "few seconds ago",
    "moments ago",
    "today",
)


def _parse_card_age_days(text: str | None) -> int | None:
    """
    Deterministically map a Naukri search-card posted-date label to an
    age in whole days. Returns None when the label is missing or not
    recognised — the discovery freshness gate treats None as "NOT known
    to be fresh" and excludes it, never as fresh. No LLM, no network.

    Recognised forms (case-insensitive, whitespace-normalised):
      "Just now" / "Few hours ago" / "Few minutes ago" / "Today"   -> 0
      "Yesterday" / "A day ago" / "1 Day Ago"                      -> 1
      "N minute(s)/hour(s) ago"                                     -> 0
      "N day(s) ago"                                                -> N
      "N+ Days Ago" (e.g. "30+ Days Ago")                           -> N
      "N week(s) ago"                                               -> N * 7
      "N month(s) ago"                                              -> N * 30
      "N year(s) ago"                                               -> N * 365
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip().lower())
    if not t:
        return None
    if any(marker in t for marker in _AGE_ZERO_MARKERS):
        return 0
    if t in ("yesterday", "a day ago", "one day ago"):
        return 1
    m = re.search(
        r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago", t
    )
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit in ("minute", "min", "hour", "hr"):
        return 0
    if unit == "day":
        return n
    if unit == "week":
        return n * 7
    if unit == "month":
        return n * 30
    return n * 365  # unit == "year"


def build_query_matrix(profile: CandidateProfile, settings: Settings) -> list[DiscoveryQuery]:
    if settings.discovery_queries:
        out: list[DiscoveryQuery] = []
        for raw in settings.discovery_queries:
            if "@" in raw:
                role, loc = raw.split("@", 1)
                out.append(DiscoveryQuery(role=role.strip(), location=loc.strip()))
            else:
                out.append(DiscoveryQuery(role=raw.strip()))
        return out
    roles = list(profile.preferred_roles) or ([profile.full_name] if False else [])
    locations = list(profile.preferred_locations) or [""]
    matrix = [DiscoveryQuery(role=r, location=l) for r in roles for l in locations]
    return matrix


def discover_and_store(
    session: Session,
    client,
    profile: CandidateProfile,
    settings: Settings,
    *,
    run_id: int,
    seq_start: int = 0,
) -> tuple[DiscoveryResult, int]:
    """Returns (result, next_seq)."""
    seq = seq_start
    matrix = build_query_matrix(profile, settings)
    queries_run = 0
    queries_failed = 0
    seen_urls: list[str] = []
    # url -> raw search-card posted-date label (first sighting wins, so
    # this mirrors the URL-dedup "first occurrence wins" rule below).
    posted_by_url: dict[str, str | None] = {}

    for q in matrix:
        queries_run += 1
        try:
            summaries = client.search_jobs(q.role, q.location)
        except Exception as exc:  # noqa: BLE001
            queries_failed += 1
            seq += 1
            add_run_event(
                session,
                daily_run_id=run_id,
                seq=seq,
                stage="discover_query",
                status=RunEventStatus.FAILED,
                detail={"query": f"{q.role} @ {q.location}", "error": type(exc).__name__},
            )
            continue
        picked = [s for s in summaries if s.url][: settings.discovery_max_jobs_per_query]
        for s in picked:
            seen_urls.append(s.url)
            posted_by_url.setdefault(s.url, s.posted_text)
        seq += 1
        add_run_event(
            session,
            daily_run_id=run_id,
            seq=seq,
            stage="discover_query",
            status=RunEventStatus.OK,
            detail={"query": f"{q.role} @ {q.location}", "results": len(picked)},
        )

    # de-dup (first occurrence wins -> preserves Naukri's query ordering),
    # then apply the hard discovery ceiling.
    ordered_unique: list[str] = []
    seen: set[str] = set()
    for u in seen_urls:
        if u not in seen:
            seen.add(u)
            ordered_unique.append(u)
    discovered_card_count = len(ordered_unique)
    ordered_unique = ordered_unique[: settings.discovery_max_total_jobs]

    # --- Phase F1: freshness-first gate (deterministic; no LLM) --------
    # Keep only cards whose posted-date label parses to <= the freshness
    # window, sort them newest -> oldest (stable, so equal-age jobs keep
    # Naukri's query order), and cap the survivors. Absent/unparseable
    # labels are EXCLUDED, never assumed fresh, and counted here.
    window_days = settings.discovery_freshness_days
    aged: list[tuple[str, int]] = []
    unknown_freshness = 0
    stale_excluded = 0
    for u in ordered_unique:
        age = _parse_card_age_days(posted_by_url.get(u))
        if age is None:
            unknown_freshness += 1
            continue
        if age > window_days:
            stale_excluded += 1
            continue
        aged.append((u, age))
    fresh_sorted = [u for u, _age in sorted(aged, key=lambda pair: pair[1])]
    fresh_capped = fresh_sorted[: settings.discovery_fresh_job_limit]

    seq += 1
    add_run_event(
        session,
        daily_run_id=run_id,
        seq=seq,
        stage="discover_freshness",
        status=RunEventStatus.OK,
        detail={
            "discovered_cards": discovered_card_count,
            "window_days": window_days,
            "within_window": len(aged),
            "stale_excluded": stale_excluded,
            "unknown_excluded": unknown_freshness,
            "fresh_cap": settings.discovery_fresh_job_limit,
            "selected": len(fresh_capped),
        },
    )

    ordered_unique = fresh_capped

    jobs_new = 0
    jobs_reseen = 0
    details_failed = 0
    job_ids: list[int] = []

    for url in ordered_unique:
        try:
            detail = client.fetch_job_detail(url)
        except Exception as exc:  # noqa: BLE001
            details_failed += 1
            seq += 1
            add_run_event(
                session,
                daily_run_id=run_id,
                seq=seq,
                stage="fetch_detail",
                status=RunEventStatus.FAILED,
                detail={"error": type(exc).__name__},
            )
            continue
        job_create = JobCreate(
            title=detail.title or "(unknown title)",
            company=detail.company or "(unknown company)",
            location=detail.location or "",
            salary_text=detail.salary_text,
            experience_text=detail.experience_text,
            description=detail.description or "",
            url=detail.url,
            posted_date_text=detail.posted_date_text,
            source=detail.source,
        )
        job, created = upsert_job(session, job_create, run_id=run_id)
        # Raw skill evidence (ld+json `skills` / Key Skills DOM chips) --
        # replaces the job's current snapshot, same job_id (never
        # canonicalized), same "overwrite in place on re-fetch"
        # convention Job's own raw fields already use. Never scored or
        # read by the LLM prompt; see jobs/skill_evidence.py for the
        # derived layer this feeds.
        replace_raw_skill_evidence(
            session,
            job.id,
            ld_json_skills=detail.ld_json_skills,
            key_skills_dom=detail.key_skills_dom,
        )
        job_ids.append(job.id)
        if created:
            jobs_new += 1
        else:
            jobs_reseen += 1
        seq += 1
        add_run_event(
            session,
            daily_run_id=run_id,
            seq=seq,
            stage="fetch_detail",
            status=RunEventStatus.OK if (detail.description or detail.title) else RunEventStatus.PARTIAL,
            job_id=job.id,
            detail={"created": created, "has_description": bool(detail.description)},
        )

    total_failure = queries_run > 0 and queries_failed == queries_run and not job_ids
    result = DiscoveryResult(
        queries_run=queries_run,
        queries_failed=queries_failed,
        jobs_new=jobs_new,
        jobs_reseen=jobs_reseen,
        details_failed=details_failed,
        job_ids=job_ids,
        total_failure=total_failure,
    )
    return result, seq
