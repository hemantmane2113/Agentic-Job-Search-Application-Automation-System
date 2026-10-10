"""
run_daily_recommendations: the daily match-digest business logic.

Contains NO scheduling. Sequence:
  load profile/resume/registry -> upsert candidate -> DailyRun(STARTED)
  -> discovery (read-only) -> per-job parse(LLM) + score(deterministic)
  + resume-select(static) -> build_digest (application-aware, capped)
  -> render + send (file/console by default; real SMTP only if
     EMAIL_SENDER=smtp is explicitly configured, see notifications/
     email.py) -> Excel export from DB -> finalise DailyRun +
     RunEvent trail.

The default EMAIL_SENDER is still "file", so nothing sends real email
unless explicitly opted in.
"""

from __future__ import annotations

import datetime
import logging

from pydantic import BaseModel

from naukri_agent.candidate.models import load_candidate_profile
from naukri_agent.config import Settings, get_settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import DailyRun, DailyRunStatus, JobRawSkillEvidence, RunEventStatus
from naukri_agent.database.repositories import (
    add_job_extraction_skill_evidence,
    add_run_event,
    upsert_candidate,
    upsert_job_match,
    upsert_resume_selection,
)
from naukri_agent.jobs.parser import JobParser
from naukri_agent.jobs.skill_evidence import build_skill_evidence, merged_required_preferred
from naukri_agent.matching.scorer import score_job
from naukri_agent.orchestration.discovery import DiscoveryResult
from naukri_agent.recommendations.builder import (
    applied_via_agent_since,
    build_digest,
    company_site_status_since,
    previous_digest_time,
)
from naukri_agent.reporting.excel import export_workbook
from naukri_agent.resume.models import load_master_resume
from naukri_agent.resume.registry import load_resume_registry
from naukri_agent.resume.selector import select_resume

logger = logging.getLogger(__name__)


class _RawKeySkillChip:
    """Duck-typed stand-in for browser.models.KeySkillChip, built from a
    persisted JobRawSkillEvidence row so jobs.skill_evidence.evidence_
    from_key_skills_dom() (which only needs `.text`/`.preferred`) can
    be reused without importing browser/ into the pipeline."""

    def __init__(self, text: str, preferred: bool) -> None:
        self.text = text
        self.preferred = preferred


class DailyRunResult(BaseModel):
    run_id: int
    status: str
    jobs_discovered: int
    jobs_evaluated: int
    recommendations: int
    email_status: str
    excel_path: str | None = None
    email_path: str | None = None
    failure_reason: str | None = None
    notes: list[str] = []


_RETRYABLE_PARSE_ERRORS = ("llm_error", "invalid_json", "invalid_schema")


def _parse_with_retry(provider, job, budget: list[int]):
    """
    Parse once; if it failed in a way a second attempt can fix (timeout, malformed or schema-
    invalid reply) and the run's retry budget allows, parse ONE more time. Returns
    (result, retried). An oversized description ("jd_too_long") is deterministic, so it is
    never retried. The budget is a one-item list shared across the whole run.
    """
    pr = JobParser(provider).parse(job)
    if pr.success or budget[0] <= 0 or not (pr.error or "").startswith(_RETRYABLE_PARSE_ERRORS):
        return pr, False
    budget[0] -= 1
    logger.info("retrying parse once for job id=%s (%s)", job.id, (pr.error or "")[:40])
    return JobParser(provider).parse(job), True


def _telegram_slots(session, candidate_id, settings, now) -> int:
    """
    How many Naukri-Apply applications today's budget reserves: the ones actually ready for telegram-apply, up to
    what is still allowed in the rolling 24 hours. The rest of daily_job_total goes to the 'apply yourself' list,
    so on a day with few Naukri-Apply jobs that list grows (never past daily_recommendation_limit).
    """
    try:
        from naukri_agent.database.repositories import auto_apply_count_since
        from naukri_agent.recommendations.apply_ready import select_candidates

        session.flush()
        ready = len(select_candidates(session, candidate_id, settings, now))
        remaining = max(0, settings.auto_apply_daily_cap - auto_apply_count_since(session, now - datetime.timedelta(hours=24)))
        return min(ready, remaining)
    except Exception as exc:  # noqa: BLE001 - if unsure, reserve the full Telegram share rather than overfill the list
        logger.warning("could not work out today's Telegram slots: %s", type(exc).__name__)
        return settings.auto_apply_daily_cap


def _refresh_note(result) -> str | None:
    """One line for the email, or None when there is nothing worth saying."""
    outcome, rid = result.outcome, result.resume_id
    if outcome == "uploaded":
        return f"Profile refreshed: uploaded {rid} (Naukri shows {result.after_filename})."
    if outcome == "already_done":
        return f"Profile already refreshed today ({rid})."
    if outcome == "unconfirmed":
        return f"Profile refresh: uploaded {rid} but Naukri did not show it afterwards - please check your profile."
    if outcome == "needs_human":
        return "Profile refresh skipped: Naukri asked for a CAPTCHA/OTP. Nothing was changed."
    if outcome == "failed":
        return f"Profile refresh failed ({result.detail or 'unknown error'}). Nothing was changed."
    if outcome == "paused":
        return "Profile refresh is paused (pause file present)."
    return None  # disabled / nothing_to_do


def _refresh_profile_first(settings, session_factory, now, refresh_fn) -> str | None:
    try:
        if refresh_fn is None:
            from naukri_agent.orchestration.profile_refresh import run_profile_refresh

            notify = None
            if settings.profile_refresh_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
                from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

                notify = build_telegram_interaction(settings).notify
            result = run_profile_refresh(
                settings, execute=True, session_factory=session_factory, now=now, notify=notify
            )
        else:
            result = refresh_fn(settings, session_factory, now)
        return _refresh_note(result)
    except Exception as exc:  # noqa: BLE001 - the job search must go on whatever happened here
        logger.warning("profile refresh step crashed: %s", type(exc).__name__)
        return f"Profile refresh failed ({type(exc).__name__}). Nothing was changed."


def _default_discover(session, profile, settings, run_id, seq):
    """Real discovery: launch the browser, log in (CAPTCHA/MFA stays
    human-in-the-loop), search + fetch details. Not exercised by unit
    tests (they inject discover_fn)."""
    from naukri_agent.browser.browser_manager import BrowserManager
    from naukri_agent.browser.naukri_client import NaukriClient
    from naukri_agent.orchestration.discovery import discover_and_store

    with BrowserManager(settings) as browser:
        client = NaukriClient(browser.page, settings)
        client.login()
        return discover_and_store(
            session, client, profile, settings, run_id=run_id, seq_start=seq
        )


def _build_extraction_provider(settings: Settings):
    if not settings.llm_provider_is_configured():
        return None
    try:
        from naukri_agent.llm.factory import get_default_llm_provider

        return get_default_llm_provider(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build LLM provider (%s); scoring without extractions", exc)
        return None


def run_daily_recommendations(
    settings: Settings | None = None,
    *,
    now: datetime.datetime | None = None,
    discover_fn=None,
    extraction_provider=None,
    explain_provider=None,
    session_factory=None,
    profile_refresh_fn=None,
    telegram_notify=None,
) -> DailyRunResult:
    settings = settings or get_settings()
    now = now or datetime.datetime.now(datetime.UTC)
    discover_fn = discover_fn or _default_discover
    session_factory = session_factory or init_db(settings)

    # FIRST STEP of the day: refresh the Naukri profile (upload the next resume in rotation) so it
    # is fresh before anything else happens. Off unless PROFILE_REFRESH_ENABLED=true. It runs before
    # the run record / long DB transaction below, and whatever happens it can never stop the job
    # search: any failure is reduced to a one-line note in the email.
    refresh_note = _refresh_profile_first(settings, session_factory, now, profile_refresh_fn)

    with session_scope(session_factory) as session:
        profile = load_candidate_profile(settings.candidate_profile_path)
        resume = load_master_resume(settings.master_resume_path)
        registry = load_resume_registry(settings.resume_registry_path)
        candidate = upsert_candidate(session, profile)
        session.flush()

        run = DailyRun(status=DailyRunStatus.STARTED, started_at=now)
        session.add(run)
        session.flush()
        run_id = run.id
        seq = 0
        notes: list[str] = []

        # --- discovery (read-only) ---
        try:
            discovery, seq = discover_fn(session, profile, settings, run_id, seq)
        except Exception as exc:  # noqa: BLE001
            seq += 1
            add_run_event(
                session, daily_run_id=run_id, seq=seq, stage="discover",
                status=RunEventStatus.FAILED, detail={"error": type(exc).__name__},
            )
            run.status = DailyRunStatus.FAILED
            run.failure_reason = f"discovery failed: {type(exc).__name__}"
            run.finished_at = datetime.datetime.now(datetime.UTC)
            return DailyRunResult(
                run_id=run_id, status=run.status.value, jobs_discovered=0,
                jobs_evaluated=0, recommendations=0, email_status="not_attempted",
                failure_reason=run.failure_reason,
            )

        if isinstance(discovery, DiscoveryResult) and discovery.total_failure:
            run.status = DailyRunStatus.FAILED
            run.failure_reason = "discovery: all queries failed"
            run.finished_at = datetime.datetime.now(datetime.UTC)
            seq += 1
            add_run_event(
                session, daily_run_id=run_id, seq=seq, stage="discover",
                status=RunEventStatus.FAILED, detail={"queries_failed": discovery.queries_failed},
            )
            return DailyRunResult(
                run_id=run_id, status=run.status.value, jobs_discovered=0,
                jobs_evaluated=0, recommendations=0, email_status="not_attempted",
                failure_reason=run.failure_reason,
            )

        job_ids = list(discovery.job_ids)
        run.jobs_discovered = discovery.jobs_new

        # --- per-job: parse (LLM, optional) + score (deterministic) + resume select ---
        if extraction_provider is None:
            extraction_provider = _build_extraction_provider(settings)
        parse_failures = 0
        retry_budget = [max(0, settings.parse_retries_per_run)]
        # Job ids whose LLM parse was ATTEMPTED this run and FAILED. These
        # jobs are still scored (score_job(job, None, ...)) and still get a
        # diagnostic JobMatch row, but a raw-listing score can only inflate
        # the result, so they must never be recommendation-eligible. This is
        # distinct from "no extraction provider configured" (parse never
        # attempted), which is left untouched.
        parse_failed_ids: set[int] = set()
        scored = 0
        for jid in job_ids:
            from naukri_agent.database.models import Job

            job = session.get(Job, jid)
            if job is None:
                continue
            extraction_row = None
            if extraction_provider is not None:
                # Efficiency: a job that keeps reappearing in search
                # results (the common case -- most listings stay live for
                # days/weeks) has IDENTICAL JD text run after run. Re-
                # sending that same text to the LLM every day would burn
                # tokens for extraction output that can't have changed.
                # Reuse the current extraction, with no LLM call, when its
                # source_content_fingerprint still matches this job's
                # CURRENT content_fingerprint; a row written before this
                # field existed is NULL here, which never matches and
                # safely falls through to a real re-parse below.
                from naukri_agent.database.repositories import current_job_extraction

                cached = current_job_extraction(session, job.id)
                reused_cached = cached is not None and cached.source_content_fingerprint == job.content_fingerprint

                if reused_cached:
                    extraction_row = cached
                    seq += 1
                    add_run_event(session, daily_run_id=run_id, seq=seq, stage="parse",
                                  status=RunEventStatus.OK, job_id=job.id,
                                  detail={"reused_cached_extraction": True, "extraction_id": cached.id})
                else:
                    pr, retried = _parse_with_retry(extraction_provider, job, retry_budget)
                    norm_detail: dict = {}
                    if retried:
                        norm_detail["retried"] = True
                    if pr.normalizations:
                        norm_detail["normalized"] = pr.normalizations
                    if pr.warnings:
                        norm_detail["skill_warnings"] = pr.warnings
                    if pr.skill_cleanups:
                        norm_detail["skill_cleanups"] = pr.skill_cleanups
                    if pr.success and pr.extraction is not None:
                        from naukri_agent.database.repositories import add_job_extraction

                        # Hybrid skill evidence (2026-09-11): merge the LLM's
                        # required/preferred lists with this job's raw skill
                        # evidence (ld_json/Key Skills DOM, persisted during
                        # discovery -- see replace_raw_skill_evidence) and
                        # deterministic candidate-vocabulary recovery against
                        # the full JD body. required_skills/preferred_skills
                        # keep their EXACT existing name/shape/semantics on
                        # JobExtraction -- only the VALUE that goes into them
                        # changes, from merged_required_preferred(). No
                        # scraper-specific logic lives in matching/scorer.py;
                        # this stays entirely in the extraction step.
                        raw_skill_rows = (
                            session.query(JobRawSkillEvidence).filter_by(job_id=job.id).all()
                        )
                        ld_json_skills = [
                            r.skill_text for r in raw_skill_rows if r.source == "ld_json"
                        ]
                        key_skills_dom = [
                            _RawKeySkillChip(r.skill_text, bool(r.preferred))
                            for r in raw_skill_rows
                            if r.source == "key_skills_dom"
                        ]
                        _raw_evidence, merged_evidence = build_skill_evidence(
                            required_skills=pr.extraction.required_skills,
                            preferred_skills=pr.extraction.preferred_skills,
                            ld_json_skills=ld_json_skills,
                            key_skills_dom=key_skills_dom,
                            description=job.description,
                            candidate_skills=profile.skills,
                        )
                        required_out, preferred_out = merged_required_preferred(merged_evidence)
                        pr.extraction = pr.extraction.model_copy(
                            update={"required_skills": required_out, "preferred_skills": preferred_out}
                        )

                        extraction_row = add_job_extraction(
                            session, job.id, pr.extraction,
                            source_content_fingerprint=job.content_fingerprint,
                        )
                        add_job_extraction_skill_evidence(session, extraction_row.id, merged_evidence)
                        seq += 1
                        add_run_event(session, daily_run_id=run_id, seq=seq, stage="parse",
                                      status=RunEventStatus.OK, job_id=job.id,
                                      detail=norm_detail or None)
                    else:
                        parse_failures += 1
                        parse_failed_ids.add(job.id)
                        seq += 1
                        add_run_event(session, daily_run_id=run_id, seq=seq, stage="parse",
                                      status=RunEventStatus.FAILED, job_id=job.id,
                                      detail={"error": (pr.error or "unknown")[:120], **norm_detail})
            result = score_job(job, extraction_row, profile, resume, settings, now=now)
            upsert_job_match(
                session, candidate.id, job.id, result,
                job_extraction_id=extraction_row.id if extraction_row else None,
            )
            outcome = select_resume(job, extraction_row, registry, provider=None)
            upsert_resume_selection(session, job.id, outcome, candidate_id=candidate.id)
            scored += 1
            seq += 1
            add_run_event(session, daily_run_id=run_id, seq=seq, stage="score",
                          status=RunEventStatus.OK, job_id=job.id,
                          detail={"score": result.overall_score, "decision": result.decision.value})
        run.jobs_evaluated = scored
        if parse_failures:
            notes.append(
                f"{parse_failures} job(s) could not be parsed by the LLM and were scored "
                "on the raw listing only."
            )

        # --- digest (application-aware, capped) ---
        session.flush()
        telegram_slots = _telegram_slots(session, candidate.id, settings, now)
        part1_limit = min(settings.daily_recommendation_limit, max(0, settings.daily_job_total - telegram_slots))
        digest = build_digest(
            session,
            candidate_id=candidate.id,
            candidate_email=profile.email,
            scored_job_ids=job_ids,
            settings=settings,
            run_id=run_id,
            now=now,
            explain_provider=(explain_provider if settings.explanation_use_llm else None),
            parse_failed_job_ids=parse_failed_ids,
            manual_apply_only=True,  # Part 1 = company-website jobs; Naukri-Apply jobs go via telegram-apply
            limit=part1_limit,
        )
        digest.telegram_slots = telegram_slots
        digest.part1_limit = part1_limit
        digest.daily_total = settings.daily_job_total
        digest.applied_via_agent = applied_via_agent_since(  # Part 2 = what the app applied to since the last digest
            session, previous_digest_time(session, run_id, now)
        )
        digest.company_site_status = company_site_status_since(  # Part 3 = where recent company-website jobs stand
            session, now, settings.followup_lookback_days, {r.job_id for r in digest.recommendations}
        )
        digest.notes.extend(notes)
        digest.profile_refresh_note = refresh_note
        seq += 1
        add_run_event(session, daily_run_id=run_id, seq=seq, stage="build_digest",
                      status=RunEventStatus.OK,
                      detail={"count": digest.count, "eligible": digest.eligible_count})

        # --- render + send (file/console only in Stage A) ---
        from naukri_agent.notifications.email import build_email_sender
        from naukri_agent.notifications.exceptions import EmailSendError
        from naukri_agent.notifications.render import render_digest

        message = render_digest(digest, settings)
        email_status = "dry_run" if settings.dry_run else "written"
        email_path = None
        try:
            # build_email_sender() lives inside this try too: a
            # misconfigured email_sender="smtp" (missing SMTP_HOST/
            # SMTP_USERNAME/SMTP_PASSWORD/NOTIFY_EMAIL_TO) raises
            # EmailConfigError, a subclass of EmailSendError, so a
            # scheduled run with an incomplete .env fails this run
            # cleanly below instead of the exception escaping
            # run_daily_recommendations() uncaught.
            sender = build_email_sender(settings)
            send_result = sender.send(message)
            email_path = send_result.path
            for row in (
                session.query(_jr()).filter_by(daily_run_id=run_id).all()
            ):
                row.email_status = "dry_run" if settings.dry_run else "written"
            seq += 1
            add_run_event(session, daily_run_id=run_id, seq=seq, stage="send_email",
                          status=RunEventStatus.OK,
                          detail={"sender": send_result.sender, "dry_run": settings.dry_run})
        except EmailSendError as exc:
            email_status = "send_failed"
            for row in session.query(_jr()).filter_by(daily_run_id=run_id).all():
                row.email_status = "send_failed"
            seq += 1
            add_run_event(session, daily_run_id=run_id, seq=seq, stage="send_email",
                          status=RunEventStatus.FAILED, detail={"error": type(exc).__name__})
            run.status = DailyRunStatus.FAILED
            run.failure_reason = f"email delivery failed: {type(exc).__name__}"

        # --- finalise (BEFORE Excel export: the "Daily Runs" sheet reads
        # run.status/run.finished_at directly off this same in-memory row,
        # so finalising after export_workbook() would make every
        # successful run show as "STARTED" forever in the exported file,
        # even though this function's own returned DailyRunResult.status
        # is correct -- that field is set below, after this point either
        # way. Excel export failing never changes run.status, so moving
        # this earlier doesn't affect failure semantics.) ---
        if run.status != DailyRunStatus.FAILED:
            run.status = DailyRunStatus.COMPLETED
        run.finished_at = datetime.datetime.now(datetime.UTC)

        # --- Excel export (regenerated from DB; never fails the run) ---
        excel_path = None
        if settings.excel_export_enabled:
            try:
                res = export_workbook(session, settings.excel_path, settings)
                excel_path = res.path
                seq += 1
                add_run_event(session, daily_run_id=run_id, seq=seq, stage="excel_export",
                              status=RunEventStatus.OK, detail={"jobs": res.jobs_rows})
            except Exception as exc:  # noqa: BLE001
                seq += 1
                add_run_event(session, daily_run_id=run_id, seq=seq, stage="excel_export",
                              status=RunEventStatus.FAILED, detail={"error": type(exc).__name__})
                notes.append(f"Excel export failed: {type(exc).__name__}")

        # --- phone ping: how many jobs are ready for telegram-apply (never applies anything) ---
        ping = _ping_apply_ready(session, candidate.id, settings, now, telegram_notify)
        if ping is not None:
            seq += 1
            add_run_event(session, daily_run_id=run_id, seq=seq, stage="telegram_ping",
                          status=RunEventStatus.OK if ping[0] else RunEventStatus.FAILED,
                          detail={"jobs": ping[1], "job_ids": ping[2]})  # the ids feed the weekly report

        return DailyRunResult(
            run_id=run_id,
            status=run.status.value,
            jobs_discovered=run.jobs_discovered,
            jobs_evaluated=run.jobs_evaluated,
            recommendations=digest.count,
            email_status=email_status,
            excel_path=excel_path,
            email_path=email_path,
            failure_reason=run.failure_reason,
            notes=notes,
        )


_PING_LIST_MAX = 5


from naukri_agent.recommendations.employment import describe as describe_employment  # noqa: E402


def _apply_ready_text(jobs: list[dict], remaining: int, cap: int, remote_start: bool = False) -> str:
    lines = [f"Naukri: {len(jobs)} job(s) ready to apply via Telegram (ACCEPT, Naukri Apply button).", ""]
    for i, j in enumerate(jobs[:_PING_LIST_MAX], 1):
        resume = j.get("resume_id") or "no role match"
        lines.append(f"{i}. {j['title']} - {j['company']}  (score {j['score']:.0f}, resume {resume}, {describe_employment(j.get('employment_type_text'))})")
    if len(jobs) > _PING_LIST_MAX:
        lines.append(f"... and {len(jobs) - _PING_LIST_MAX} more")
    lines.append("")
    if remaining > 0:
        lines.append(f"You can approve up to {remaining} today (limit {cap} per 24h).")
        start = "Send /apply here to start" if remote_start else "Start with your usual telegram-apply command"
        lines.append(f"{start}; nothing is applied until you tap Yes on each job.")
    else:
        lines.append(f"The daily limit ({cap} per 24h) is already used, so these wait for a later day.")
    return "\n".join(lines)


def _ping_apply_ready(session, candidate_id, settings, now, notify):
    """
    Tell the phone how many jobs `telegram-apply` could offer right now. Returns None when nothing
    was sent (feature off, Telegram not set up, or no jobs), else (sent_ok, job_count). Whatever
    goes wrong here is reduced to (False, n): a failed ping must never fail the daily run.
    """
    if not settings.telegram_ping_apply_ready:
        return None
    if notify is None and not (settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    try:
        from naukri_agent.database.repositories import auto_apply_count_since
        from naukri_agent.recommendations.apply_ready import select_candidates

        session.flush()
        jobs = select_candidates(session, candidate_id, settings, now)
        if not jobs:
            return None
        remaining = max(
            0, settings.auto_apply_daily_cap - auto_apply_count_since(session, now - datetime.timedelta(hours=24))
        )
        text = _apply_ready_text(jobs, remaining, settings.auto_apply_daily_cap, settings.telegram_remote_start)
        if notify is None:
            from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

            notify = build_telegram_interaction(settings).notify
        notify(text)
        return True, len(jobs), [j["job_id"] for j in jobs[:100]]
    except Exception as exc:  # noqa: BLE001 - the digest is already sent; the ping is a courtesy
        logger.warning("telegram ping failed: %s", type(exc).__name__)
        return False, 0, []


def _jr():
    from naukri_agent.database.models import JobRecommendation

    return JobRecommendation
