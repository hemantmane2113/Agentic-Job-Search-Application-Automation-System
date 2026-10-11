"""
Which jobs `telegram-apply` could offer right now, with what the phone card needs.

Read-only: a few database queries and some text parsing, no browser and no side effects. It lives
here, apart from the apply runner, so the daily run can tell the phone how many jobs are ready
without ever importing anything that can apply (a structural test guards that separation).
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from naukri_agent.config import Settings
from naukri_agent.recommendations.employment import is_excluded

_MATCHED_RE = re.compile(r"^(?P<skill>.+?) (?P<kind>required|preferred) skill matched")
_MISSING_RE = re.compile(r"^(?P<skill>.+?) (?P<kind>required|preferred) but not present$")


def split_skill_factors(positives: list[str], negatives: list[str]) -> dict[str, list[str]]:
    """The scorer's own explanation lines, split into matched / missing skills,
    required vs preferred. Pure text parsing of what the scorer already wrote;
    nothing is inferred and no skill is invented."""
    out: dict[str, list[str]] = {
        "matched_required": [], "matched_preferred": [], "missing_required": [], "missing_preferred": [],
    }
    for text in positives:
        m = _MATCHED_RE.match(text)
        if m:
            out[f"matched_{m.group('kind')}"].append(m.group("skill").strip())
    for text in negatives:
        m = _MISSING_RE.match(text)
        if m:
            out[f"missing_{m.group('kind')}"].append(m.group("skill").strip())
    return out


def select_candidates(session: Any, candidate_id: int, settings: Settings, now: datetime.datetime) -> list[dict]:
    """Eligible jobs, best score first. Pure DB reads."""
    from naukri_agent.database.models import Job, JobExtraction, JobMatch, ResumeSelection
    from naukri_agent.database.repositories import application_status_for_job, auto_apply_job_ids_to_skip
    from naukri_agent.resume.selector import ResumeSelectionDecision
    from naukri_agent.matching.models import MatchDecision

    decisions = [MatchDecision(d.upper()) for d in settings.auto_apply_decisions]
    cutoff = now - datetime.timedelta(days=settings.auto_apply_max_job_age_days)
    excluded = {s.upper() for s in settings.recommendation_exclude_if_status}
    skip = auto_apply_job_ids_to_skip(session, now)

    rows = (
        session.query(JobMatch, Job)
        .join(Job, Job.id == JobMatch.job_id)
        .filter(
            JobMatch.candidate_id == candidate_id,
            JobMatch.decision.in_(decisions),
            JobMatch.job_extraction_id.isnot(None),
            Job.apply_type == "native",
            Job.repost_of_job_id.is_(None),
            Job.last_seen_at >= cutoff,
        )
        .order_by(JobMatch.overall_score.desc())
        .all()
    )
    picked = []
    for match, job in rows:
        if job.id in skip:
            continue
        if is_excluded(job.employment_type_text, settings.employment_filter_enabled, title=job.title):
            continue
        if application_status_for_job(session, job.id).name in excluded:
            continue
        extraction = session.get(JobExtraction, match.job_extraction_id)
        # The resume chosen for this job's role (one of the user's own three). None = no role match.
        chosen = (
            session.query(ResumeSelection)
            .filter_by(candidate_id=candidate_id, job_id=job.id, decision=ResumeSelectionDecision.SELECTED)
            .one_or_none()
        )
        picked.append(
            {"job_id": job.id, "title": job.title, "company": job.company, "url": job.url,
             "score": match.overall_score,
             "employment_type_text": job.employment_type_text,
             "resume_id": chosen.resume_id if chosen else None,
             "resume_file": chosen.file_path if chosen else None,
             "resume_hash": chosen.file_hash if chosen else None,
             "experience_text": job.experience_text,
             "experience_min": extraction.experience_min if extraction else None,
             "experience_max": extraction.experience_max if extraction else None,
             **split_skill_factors(list(match.positive_factors or []), list(match.negative_factors or []))}
        )
    return picked
