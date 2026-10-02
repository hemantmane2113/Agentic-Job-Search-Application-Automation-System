"""Phase 15: EmailOutreachAttempt repository functions -- insert/finalize
round-trip and count_emails_sent_today's UTC-calendar-day boundary."""

import datetime

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import EmailOutreachMode, EmailOutreachStatus
from naukri_agent.database.repositories import (
    add_email_outreach_attempt,
    count_emails_sent_today,
    finalize_email_outreach_attempt,
)

from .digest_fakes import add_job, in_memory_factory


def test_add_then_finalize_sent_round_trips():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000300")
        attempt = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-1", mode=EmailOutreachMode.APPLICATION,
            recipient_email="hr@acme.com", drafted_subject="Application", drafted_body="body",
            resume_id="r1", resume_file_path="resumes/r1.pdf", llm_provider="fake", llm_model="m",
        )
        assert attempt.status == EmailOutreachStatus.DRAFTED
        assert attempt.sent_at is None

        finalize_email_outreach_attempt(
            s, attempt, status=EmailOutreachStatus.SENT,
            final_subject="Application", final_body="body", human_edited=False,
            application_id=42,
        )
        assert attempt.status == EmailOutreachStatus.SENT
        assert attempt.sent_at is not None
        assert attempt.application_id == 42
        assert attempt.final_subject == "Application"


def test_add_then_finalize_aborted_keeps_draft_on_record():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000301")
        attempt = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-2", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="queries@acme.com", drafted_subject="Interested", drafted_body="body",
        )
        finalize_email_outreach_attempt(
            s, attempt, status=EmailOutreachStatus.ABORTED,
            aborted_reason="user declined final confirmation",
        )
        assert attempt.status == EmailOutreachStatus.ABORTED
        assert attempt.sent_at is None
        assert attempt.application_id is None
        # the original draft is never erased
        assert attempt.drafted_subject == "Interested"
        assert attempt.aborted_reason == "user declined final confirmation"


def test_count_emails_sent_today_utc_calendar_day_boundary():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000302")

        reference = datetime.datetime(2026, 9, 20, 12, 0, 0, tzinfo=datetime.UTC)

        yesterday_late = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-y", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="a@acme.com", drafted_subject="s", drafted_body="b",
        )
        finalize_email_outreach_attempt(s, yesterday_late, status=EmailOutreachStatus.SENT)
        yesterday_late.sent_at = datetime.datetime(2026, 9, 19, 23, 59, 59, tzinfo=datetime.UTC)

        today_early = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-t1", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="b@acme.com", drafted_subject="s", drafted_body="b",
        )
        finalize_email_outreach_attempt(s, today_early, status=EmailOutreachStatus.SENT)
        today_early.sent_at = datetime.datetime(2026, 9, 20, 0, 0, 0, tzinfo=datetime.UTC)

        today_late = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-t2", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="c@acme.com", drafted_subject="s", drafted_body="b",
        )
        finalize_email_outreach_attempt(s, today_late, status=EmailOutreachStatus.SENT)
        today_late.sent_at = datetime.datetime(2026, 9, 20, 18, 0, 0, tzinfo=datetime.UTC)

        drafted_only = add_email_outreach_attempt(
            s, job_id=job.id, attempt_id="att-d", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="d@acme.com", drafted_subject="s", drafted_body="b",
        )
        s.flush()

        count = count_emails_sent_today(s, now=reference)
        assert count == 2  # today_early + today_late only
