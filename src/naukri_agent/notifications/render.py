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


def _apply_line(rec: Recommendation) -> str | None:
    """How to apply, from the page's own apply button. Nothing is shown when
    it was never checked or the page had no apply control."""
    if rec.apply_type == "company_site":
        return "   How to apply: ON THE COMPANY'S WEBSITE - apply manually (use the link below, then run mark-applied)"
    if rec.apply_type == "native":
        return "   How to apply: Naukri Apply button"
    return None


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
    apply_line = _apply_line(rec)
    if apply_line:
        lines.append(apply_line)
    lines.append(f"   Naukri: {rec.naukri_url}")
    return "\n".join(lines)


def _local_time(moment, tz_name: str) -> str | None:
    """A stored (naive UTC) timestamp as local wall-clock time, e.g. '08 Oct 2026, 01:27 PM'."""
    if moment is None:
        return None
    import datetime as _dt

    try:
        from zoneinfo import ZoneInfo

        local = moment.replace(tzinfo=_dt.UTC).astimezone(ZoneInfo(tz_name))
        return local.strftime("%d %b %Y, %I:%M %p")
    except Exception:  # noqa: BLE001 - no tz database: say so rather than show a wrong clock
        return moment.strftime("%d %b %Y, %H:%M UTC")


def _applied_block(i: int, a, tz_name: str) -> str:
    lines = [f"{i}. {a.job_title} — {a.company}"]
    if a.location:
        lines.append(f"   Location: {a.location}")
    when = _local_time(a.applied_at, tz_name)
    if when:
        lines.append(f"   Applied: {when}")
    for question, answer in a.answers:
        lines.append(f"   Q: {question}")
        lines.append(f"   A: {answer}")
    lines.append(f"   Naukri: {a.job_url}")
    return "\n".join(lines)


def _render_two_part(digest: RecommendationDigest, settings: Settings) -> EmailMessage:
    date_str = digest.run_date.strftime("%d %b %Y")
    n1, n2 = digest.count, len(digest.applied_via_agent)
    subject = f"{settings.email_subject_prefix} {n1} to apply yourself, {n2} applied for you — {date_str}"

    part1 = [f"PART 1 — APPLY YOURSELF ON THE COMPANY'S WEBSITE ({n1})"]
    if n1:
        part1.append(
            "These match your criteria but only offer \"Apply on company site\". Open each link, "
            "press that button on Naukri, and apply on the company's own website. Afterwards record it "
            "with: naukri-agent mark-applied <job>."
        )
        if digest.truncated:
            part1.append(
                f"Note: {digest.eligible_count - digest.count} more eligible job(s) not shown "
                f"(daily limit {digest.limit})."
            )
    else:
        part1.append("None today.")
    for note in digest.notes:
        part1.append(f"Note: {note}")

    part2 = [f"PART 2 — APPLIED FOR YOU VIA TELEGRAM ({n2}), since the last digest"]
    if n2:
        part2.append("")
        part2.append("\n\n".join(_applied_block(i, a, settings.timezone) for i, a in enumerate(digest.applied_via_agent, 1)))
    else:
        part2.append("Nothing was applied via Telegram since the last digest.")

    sections = [f"Daily Naukri job digest — {date_str}"]
    if digest.profile_refresh_note:
        sections.append(digest.profile_refresh_note)
    sections.append("\n".join(part1))
    sections += [_block(r) for r in digest.recommendations]
    sections.append("\n".join(part2))
    if digest.native_waiting:
        s = "" if digest.native_waiting == 1 else "s"
        sections.append(
            f"{digest.native_waiting} more matching job{s} with a Naukri Apply button "
            f"{'is' if digest.native_waiting == 1 else 'are'} waiting for you. Run: naukri-agent telegram-apply"
        )
    body = "\n\n".join(sections) + (
        "\n\n---\n"
        "Part 1 applications are manual. Part 2 were made by the app only after you tapped Yes on Telegram.\n"
        "Application status is from your local database only.\n"
    )
    return EmailMessage(to=digest.candidate_email, subject=subject, text_body=body)


def render_digest(digest: RecommendationDigest, settings: Settings) -> EmailMessage:
    if digest.two_part:
        return _render_two_part(digest, settings)
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
