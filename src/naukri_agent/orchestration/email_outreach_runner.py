"""
Phase 15: run_email_outreach_workflow — pipeline logic for the
`email-outreach` CLI command. Same role orchestration/apply_runner.py
plays for `apply`; deliberately a SEPARATE module with NO
BrowserManager/NaukriClient/Playwright import anywhere, since sending an
email needs no Naukri browser session at all.

Flow: resolve job → read its JobExtraction.email_application_signal
(pure, LLM-produced extraction) → Python alone maps the signal to a
mode (APPLY_VIA_EMAIL -> APPLICATION, CONTACT_ONLY -> COLD_OUTREACH,
NONE -> abort) → daily-cap check → resolve resume → draft via
agents/cold_email_agent.py → persist the draft immediately (status=
DRAFTED) → human reviews/edits the WHOLE draft in one pass → re-check
the daily cap (closes the gap opened by an arbitrarily long review
pause) → one more explicit "y" before the real, irreversible send →
finalize the SAME row to SENT or ABORTED. A confirmed APPLICATION-mode
send also creates/updates ApplicationHistory(source="agent_email_apply")
— COLD_OUTREACH never touches it, so a cold note can never be mistaken
for an application by the recommendation/cooldown logic.

Never reachable from run-daily/discover/scheduler. See
tests/test_orchestration_email_outreach_runner.py's structural guard
asserting the reverse: pipeline.py/discovery.py/scheduler/daemon.py
never import this module, and this module itself never imports
BrowserManager/NaukriClient/playwright.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from naukri_agent.config import Settings
from naukri_agent.database.models import EmailOutreachAttempt, EmailOutreachMode, EmailOutreachStatus
from naukri_agent.jobs.models import EmailApplicationSignal

logger = logging.getLogger(__name__)

_SIGNAL_TO_MODE = {
    EmailApplicationSignal.APPLY_VIA_EMAIL: EmailOutreachMode.APPLICATION,
    EmailApplicationSignal.CONTACT_ONLY: EmailOutreachMode.COLD_OUTREACH,
}


class EmailOutreachUserInteraction:
    """
    Injectable human-interaction seam, same shape as
    apply_runner.ApplyUserInteraction. The real CLI path uses this
    default implementation (real input()/print); tests supply their own
    canned object with the same two methods — no subclassing required.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        print_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._input = input_fn or input
        self._print = print_fn or print

    def review_and_edit(
        self, mode: EmailOutreachMode, recipient_email: str, subject: str, body: str
    ) -> tuple[str, str]:
        """
        Show the drafted subject/body and let the human accept (blank
        input) or replace each one. Returns (final_subject, final_body).
        """
        label = "application-by-email" if mode == EmailOutreachMode.APPLICATION else "cold-outreach"
        self._print(f"\n--- Review this drafted {label} to {recipient_email} before anything is sent ---")
        self._print(f"Drafted subject: {subject!r}")
        new_subject = self._input("Your subject (Enter to accept as-is): ").strip()
        self._print(f"Drafted body:\n{body}")
        new_body = self._input("Your body (Enter to accept as-is, or type a full replacement): ").strip()
        return (new_subject or subject, new_body or body)

    def confirm_send(self, summary: str) -> bool:
        """Print `summary` and require a literal 'y' before returning
        True — anything else means nothing is sent."""
        self._print(summary)
        response = self._input("\nSend this email? Type 'y' to confirm, anything else aborts: ")
        return response.strip() == "y"


class EmailOutreachResult(BaseModel):
    job_id: int
    title: str
    company: str
    mode: EmailOutreachMode | None = None
    recipient_email: str | None = None
    resume_id: str | None = None
    resume_file: str | None = None
    attempt_id: str
    human_edited: bool = False
    sent: bool = False
    application_id: int | None = None
    aborted_reason: str | None = None


def _build_summary(
    mode: EmailOutreachMode, job_title: str, company: str, recipient_email: str,
    resume_id: str | None, resume_file: str | None, subject: str, body: str,
) -> str:
    label = "Application-by-email" if mode == EmailOutreachMode.APPLICATION else "Cold outreach (NOT an application)"
    return "\n".join(
        [
            "\n--- Final summary before sending ---",
            f"Type: {label}",
            f"Job: {job_title} @ {company}",
            f"To: {recipient_email}",
            f"Resume attached: {resume_id or 'none'} ({resume_file or 'no file selected'})",
            f"Subject: {subject}",
            f"Body:\n{body}",
        ]
    )


def run_email_outreach_workflow(
    settings: Settings,
    job_ident: str,
    *,
    interaction: EmailOutreachUserInteraction | None = None,
) -> EmailOutreachResult:
    """
    Run one Phase 15 email-outreach attempt end to end. Gated at the CLI
    layer (cli/main.py's `email-outreach` command) by
    AUTO_EMAIL_OUTREACH=true AND DRY_RUN=false — this function itself
    does not re-check those, since it should also be directly callable
    from tests with a fake interaction.
    """
    if interaction is None:
        interaction = EmailOutreachUserInteraction()

    # Imported lazily, matching apply_runner.py's own convention — but
    # here it's not about deferring Playwright (there is none in this
    # module), just keeping this module importable without a DB/LLM
    # dependency for the structural guard test.
    from naukri_agent.agents.cold_email_agent import draft_application_email, draft_outreach_email
    from naukri_agent.candidate.models import load_candidate_profile
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.repositories import (
        add_email_outreach_attempt,
        count_emails_sent_today,
        current_job_extraction,
        finalize_email_outreach_attempt,
        latest_resume_selection,
        resolve_canonical_job,
        upsert_application_history,
    )
    from naukri_agent.llm.factory import get_email_llm_provider
    from naukri_agent.notifications.email import EmailMessage, build_email_sender
    from naukri_agent.notifications.exceptions import EmailSendError
    from naukri_agent.resume.models import load_master_resume

    attempt_id = uuid.uuid4().hex
    factory = init_db(settings)

    with session_scope(factory) as session:
        job = resolve_canonical_job(session, job_ident)
        if job is None:
            return EmailOutreachResult(
                job_id=-1, title="", company="", attempt_id=attempt_id,
                aborted_reason=f"No job matched {job_ident!r} (by id / external id / URL).",
            )
        job_id, job_title, company = job.id, job.title, job.company

        extraction = current_job_extraction(session, job_id)
        signal = extraction.email_application_signal if extraction else None
        contact_email = extraction.contact_email if extraction else None
        if signal is None or signal == EmailApplicationSignal.NONE or not contact_email:
            return EmailOutreachResult(
                job_id=job_id, title=job_title, company=company, attempt_id=attempt_id,
                aborted_reason="No contact email found in this job's extraction.",
            )
        mode = _SIGNAL_TO_MODE[signal]

        if count_emails_sent_today(session) >= settings.max_emails_per_day:
            return EmailOutreachResult(
                job_id=job_id, title=job_title, company=company, mode=mode,
                recipient_email=contact_email, attempt_id=attempt_id,
                aborted_reason=f"Daily email cap reached ({settings.max_emails_per_day}).",
            )

        selection = latest_resume_selection(session, job_id)
        resume_id = selection.resume_id if selection else None
        resume_file = selection.file_path if selection else None

    if resume_file and not Path(resume_file).exists():
        return EmailOutreachResult(
            job_id=job_id, title=job_title, company=company, mode=mode,
            recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
            attempt_id=attempt_id, aborted_reason=f"Resume file no longer found at {resume_file}",
        )

    candidate = load_candidate_profile(settings.candidate_profile_path)
    resume = load_master_resume(settings.master_resume_path)
    provider = get_email_llm_provider(settings)
    provider_name = getattr(provider, "provider_name", None)

    draft_fn = draft_application_email if mode == EmailOutreachMode.APPLICATION else draft_outreach_email
    draft = draft_fn(provider, job_title, company, contact_email, candidate, resume)
    if draft is None:
        return EmailOutreachResult(
            job_id=job_id, title=job_title, company=company, mode=mode,
            recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
            attempt_id=attempt_id,
            aborted_reason="The LLM could not draft an email for this job.",
        )

    with session_scope(factory) as session:
        attempt_row = add_email_outreach_attempt(
            session, job_id=job_id, attempt_id=attempt_id, mode=mode,
            recipient_email=contact_email, drafted_subject=draft.subject, drafted_body=draft.body,
            resume_id=resume_id, resume_file_path=resume_file,
            llm_provider=provider_name, llm_model=provider.model,
        )
        attempt_row_id = attempt_row.id

    final_subject, final_body = interaction.review_and_edit(mode, contact_email, draft.subject, draft.body)
    human_edited = (final_subject, final_body) != (draft.subject, draft.body)

    with session_scope(factory) as session:
        if count_emails_sent_today(session) >= settings.max_emails_per_day:
            attempt_row = session.get(EmailOutreachAttempt, attempt_row_id)
            finalize_email_outreach_attempt(
                session, attempt_row, status=EmailOutreachStatus.ABORTED,
                final_subject=final_subject, final_body=final_body, human_edited=human_edited,
                aborted_reason=f"Daily email cap reached ({settings.max_emails_per_day}).",
            )
            return EmailOutreachResult(
                job_id=job_id, title=job_title, company=company, mode=mode,
                recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
                attempt_id=attempt_id, human_edited=human_edited,
                aborted_reason=f"Daily email cap reached ({settings.max_emails_per_day}).",
            )

    summary = _build_summary(mode, job_title, company, contact_email, resume_id, resume_file, final_subject, final_body)
    confirmed = interaction.confirm_send(summary)

    if not confirmed:
        with session_scope(factory) as session:
            attempt_row = session.get(EmailOutreachAttempt, attempt_row_id)
            finalize_email_outreach_attempt(
                session, attempt_row, status=EmailOutreachStatus.ABORTED,
                final_subject=final_subject, final_body=final_body, human_edited=human_edited,
                aborted_reason="user declined final confirmation",
            )
        return EmailOutreachResult(
            job_id=job_id, title=job_title, company=company, mode=mode,
            recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
            attempt_id=attempt_id, human_edited=human_edited,
            aborted_reason="user declined final confirmation",
        )

    sender = build_email_sender(settings)
    attachments = [resume_file] if resume_file else []
    try:
        sender.send(EmailMessage(to=contact_email, subject=final_subject, text_body=final_body, attachments=attachments))
    except EmailSendError as exc:
        with session_scope(factory) as session:
            attempt_row = session.get(EmailOutreachAttempt, attempt_row_id)
            finalize_email_outreach_attempt(
                session, attempt_row, status=EmailOutreachStatus.ABORTED,
                final_subject=final_subject, final_body=final_body, human_edited=human_edited,
                aborted_reason=f"send failed: {type(exc).__name__}",
            )
        return EmailOutreachResult(
            job_id=job_id, title=job_title, company=company, mode=mode,
            recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
            attempt_id=attempt_id, human_edited=human_edited,
            aborted_reason=f"send failed: {type(exc).__name__}",
        )

    application_id: int | None = None
    with session_scope(factory) as session:
        if mode == EmailOutreachMode.APPLICATION:
            app_row, _created = upsert_application_history(
                session, job_id, resume_id=resume_id, resume_file_path=resume_file,
                source="agent_email_apply",
            )
            application_id = app_row.id

        attempt_row = session.get(EmailOutreachAttempt, attempt_row_id)
        finalize_email_outreach_attempt(
            session, attempt_row, status=EmailOutreachStatus.SENT,
            final_subject=final_subject, final_body=final_body, human_edited=human_edited,
            application_id=application_id,
        )

    return EmailOutreachResult(
        job_id=job_id, title=job_title, company=company, mode=mode,
        recipient_email=contact_email, resume_id=resume_id, resume_file=resume_file,
        attempt_id=attempt_id, human_edited=human_edited, sent=True, application_id=application_id,
    )
