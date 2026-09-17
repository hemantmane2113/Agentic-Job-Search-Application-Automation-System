"""
Render a RecommendationDigest to a plain-text EmailMessage.

Every field shown is deterministic (DB / matcher). The optional LLM
narrative is printed under a clearly separate "Note:" line; the factual
"Application status:" and "Freshness:" lines are always the DB-computed
labels. No credentials/secrets are ever rendered.
"""

from __future__ import annotations

from naukri_agent.config import Settings
from naukri_agent.notifications.email import EmailMessage
from naukri_agent.recommendations.models import Recommendation, RecommendationDigest


def _salary(rec: Recommendation) -> str:
    if rec.salary_text:
        return rec.salary_text
    if rec.salary_min is not None or rec.salary_max is not None:
        cur = rec.salary_currency or ""
        return f"{cur} {rec.salary_min or '?'}–{rec.salary_max or '?'}".strip()
    return "Not disclosed"


def _experience(rec: Recommendation) -> str:
    if rec.experience_text:
        return rec.experience_text
    if rec.experience_min is not None or rec.experience_max is not None:
        return f"{rec.experience_min or '?'}–{rec.experience_max or '?'} yrs"
    return "Not stated"


def _block(rec: Recommendation) -> str:
    lines = [
        f"{rec.rank}. {rec.job_title} — {rec.company}",
        f"   Location: {rec.location or 'Not stated'}",
        f"   Experience: {_experience(rec)}",
        f"   Salary: {_salary(rec)}",
        f"   Match score: {rec.match_score:.1f} / 100 ({rec.match_decision.value})",
        f"   Recommended resume: {rec.recommended_resume_id or 'review needed'}"
        + (f" [{rec.recommended_resume_status}]" if rec.recommended_resume_status else ""),
        f"   Application status: {rec.application_status_label}",
        f"   Freshness: {rec.freshness_label}",
    ]
    if rec.reasons:
        lines.append("   Why it matches:")
        lines += [f"     - {r}" for r in rec.reasons]
    if rec.gaps:
        lines.append("   Important gaps:")
        lines += [f"     - {g}" for g in rec.gaps]
    if rec.explanation:
        lines.append(f"   Note ({rec.explanation_source}): {rec.explanation}")
    lines.append(f"   Naukri: {rec.naukri_url}")
    return "\n".join(lines)


def render_digest(digest: RecommendationDigest, settings: Settings) -> EmailMessage:
    date_str = digest.run_date.strftime("%d %b %Y")
    subject = f"{settings.email_subject_prefix} {digest.count} job matches — {date_str}"

    header = [
        f"Daily Naukri job-match digest — {date_str}",
        f"Recommendations: {digest.count} (of {digest.eligible_count} eligible; limit {digest.limit})",
    ]
    if digest.truncated:
        header.append(
            f"Note: {digest.eligible_count - digest.count} more eligible job(s) not shown "
            f"(daily limit {digest.limit})."
        )
    for note in digest.notes:
        header.append(f"Note: {note}")
    if digest.count == 0:
        header.append("")
        header.append("No new matching jobs today.")

    body = "\n\n".join([" \n".join(header)] + [_block(r) for r in digest.recommendations])
    body += (
        "\n\n---\n"
        "Applications are manual. This digest never applies to anything on your behalf.\n"
        "Application status is from your local database only.\n"
    )
    return EmailMessage(to=digest.candidate_email, subject=subject, text_body=body)
