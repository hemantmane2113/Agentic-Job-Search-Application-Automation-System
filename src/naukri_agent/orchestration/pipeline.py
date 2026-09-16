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
from naukri_agent.recommendations.builder import build_digest
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
) -> DailyRunResult:
    settings = settings or get_settings()
    now = now or datetime.datetime.now(datetime.UTC)
    discover_fn = discover_fn or _default_discover
    session_factory = session_factory or init_db(settings)

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
                pr = JobParser(extraction_provider).parse(job)
                norm_detail: dict = {}
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

                    extraction_row = add_job_extraction(session, job.id, pr.extraction)
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
            result = score_job(job, extraction_row, profile, resume, settings)
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
        )
        digest.notes.extend(notes)
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

        # --- finalise ---
        if run.status != DailyRunStatus.FAILED:
            run.status = DailyRunStatus.COMPLETED
        run.finished_at = datetime.datetime.now(datetime.UTC)

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


def _jr():
    from naukri_agent.database.models import JobRecommendation

    return JobRecommendation
