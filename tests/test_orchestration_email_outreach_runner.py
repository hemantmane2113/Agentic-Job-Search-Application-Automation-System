"""
orchestration/email_outreach_runner.py: the Phase 15 cold-email /
apply-by-email pipeline logic. LLM/sender are faked at their
construction points (same mocked tier as test_orchestration_apply_runner.py)
so these tests exercise the orchestration logic -- database writes, the
mode decision, the daily cap, the final-confirm gate -- without any real
LLM/SMTP call.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import (
    ApplicationHistory,
    EmailOutreachAttempt,
    EmailOutreachMode,
    EmailOutreachStatus,
)
from naukri_agent.database.repositories import add_job_extraction
from naukri_agent.jobs.models import EmailApplicationSignal, JobExtractionCreate
from naukri_agent.orchestration.email_outreach_runner import (
    EmailOutreachUserInteraction,
    run_email_outreach_workflow,
)
from naukri_agent.resume.models import MasterResume

from .digest_fakes import add_job, settings as _digest_settings


def _settings(tmp_path, **over):
    return _digest_settings(tmp_path, **over)


def _add_extraction(session, job_id, *, signal: EmailApplicationSignal, contact_email: str | None):
    add_job_extraction(
        session, job_id,
        JobExtractionCreate(
            normalized_title="Data Scientist", contact_email=contact_email,
            email_application_signal=signal,
        ),
    )


def _make_job_with_signal(settings, slug, ext, *, signal, contact_email):
    factory = init_db(settings)
    with session_scope(factory) as s:
        job = add_job(s, slug=slug, ext=ext)
        job_id = job.id
        _add_extraction(s, job_id, signal=signal, contact_email=contact_email)
    return factory, job_id


class FakeInteraction(EmailOutreachUserInteraction):
    def __init__(self, confirm: bool = True, edited_subject=None, edited_body=None):
        super().__init__()
        self._confirm = confirm
        self._edited_subject = edited_subject
        self._edited_body = edited_body
        self.review_called_with = None

    def review_and_edit(self, mode, recipient_email, subject, body):
        self.review_called_with = (mode, recipient_email, subject, body)
        return (self._edited_subject or subject, self._edited_body or body)

    def confirm_send(self, summary: str) -> bool:
        return self._confirm


class FakeSender:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent_message = None

    def send(self, message):
        if self.fail:
            from naukri_agent.notifications.exceptions import EmailSendError

            raise EmailSendError("SmtpEmailSender failed: SMTPException")
        self.sent_message = message
        from naukri_agent.notifications.email import SendResult

        return SendResult(sender="fake", status="sent")


def _patch_common(monkeypatch, *, draft_subject="Subject", draft_body="Body", draft_fails=False, sender=None):
    class _FakeProvider:
        provider_name = "fake"
        model = "fake-model"

    monkeypatch.setattr(
        "naukri_agent.llm.factory.get_email_llm_provider", lambda settings: _FakeProvider()
    )
    monkeypatch.setattr(
        "naukri_agent.candidate.models.load_candidate_profile",
        lambda path: CandidateProfile(full_name="Jane Doe", email="jane@example.com", phone="123"),
    )
    monkeypatch.setattr(
        "naukri_agent.resume.models.load_master_resume",
        lambda path: MasterResume(professional_summary="Summary."),
    )

    if draft_fails:
        monkeypatch.setattr(
            "naukri_agent.agents.cold_email_agent.draft_application_email", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "naukri_agent.agents.cold_email_agent.draft_outreach_email", lambda *a, **k: None
        )
    else:
        from naukri_agent.agents.cold_email_agent import EmailDraft

        draft = EmailDraft(subject=draft_subject, body=draft_body, reason="r")
        monkeypatch.setattr(
            "naukri_agent.agents.cold_email_agent.draft_application_email", lambda *a, **k: draft
        )
        monkeypatch.setattr(
            "naukri_agent.agents.cold_email_agent.draft_outreach_email", lambda *a, **k: draft
        )

    fake_sender = sender if sender is not None else FakeSender()
    monkeypatch.setattr(
        "naukri_agent.notifications.email.build_email_sender", lambda settings: fake_sender
    )
    return fake_sender


def test_apply_via_email_writes_attempt_and_application_history(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake_sender = _patch_common(monkeypatch)
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000300", signal=EmailApplicationSignal.APPLY_VIA_EMAIL,
        contact_email="hr@acme.com",
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.sent is True
    assert result.mode == EmailOutreachMode.APPLICATION
    assert result.application_id is not None
    assert fake_sender.sent_message is not None
    assert fake_sender.sent_message.to == "hr@acme.com"

    with session_scope(factory) as s:
        attempt = s.query(EmailOutreachAttempt).one()
        assert attempt.status == EmailOutreachStatus.SENT
        assert attempt.mode == EmailOutreachMode.APPLICATION
        history = s.query(ApplicationHistory).one()
        assert history.source == "agent_email_apply"
        assert history.job_id == job_id
        assert attempt.application_id == history.id


def test_contact_only_writes_attempt_only_never_application_history(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_common(monkeypatch)
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000301", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="queries@acme.com",
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.sent is True
    assert result.mode == EmailOutreachMode.COLD_OUTREACH
    assert result.application_id is None

    with session_scope(factory) as s:
        attempt = s.query(EmailOutreachAttempt).one()
        assert attempt.mode == EmailOutreachMode.COLD_OUTREACH
        assert attempt.application_id is None
        assert s.query(ApplicationHistory).count() == 0


def test_no_signal_aborts_with_zero_llm_calls(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    draft_calls = []
    monkeypatch.setattr(
        "naukri_agent.agents.cold_email_agent.draft_application_email",
        lambda *a, **k: draft_calls.append(1),
    )
    monkeypatch.setattr(
        "naukri_agent.agents.cold_email_agent.draft_outreach_email",
        lambda *a, **k: draft_calls.append(1),
    )
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000302", signal=EmailApplicationSignal.NONE, contact_email=None,
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.sent is False
    assert "No contact email found" in result.aborted_reason
    assert draft_calls == []
    with session_scope(factory) as s:
        assert s.query(EmailOutreachAttempt).count() == 0


def test_missing_extraction_aborts_with_zero_llm_calls(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    draft_calls = []
    monkeypatch.setattr(
        "naukri_agent.agents.cold_email_agent.draft_application_email",
        lambda *a, **k: draft_calls.append(1),
    )
    factory = init_db(settings)
    with session_scope(factory) as s:
        job = add_job(s, slug="x", ext="040926000303")
        job_id = job.id

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())
    assert result.sent is False
    assert "No contact email found" in result.aborted_reason
    assert draft_calls == []


def test_daily_cap_already_hit_aborts_before_drafting(tmp_path, monkeypatch):
    settings = _settings(tmp_path, max_emails_per_day=1)
    draft_calls = []
    monkeypatch.setattr(
        "naukri_agent.agents.cold_email_agent.draft_application_email",
        lambda *a, **k: draft_calls.append(1),
    )
    monkeypatch.setattr(
        "naukri_agent.agents.cold_email_agent.draft_outreach_email",
        lambda *a, **k: draft_calls.append(1),
    )
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000304", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="a@acme.com",
    )
    from naukri_agent.database.repositories import add_email_outreach_attempt, finalize_email_outreach_attempt

    with session_scope(factory) as s:
        attempt = add_email_outreach_attempt(
            s, job_id=job_id, attempt_id="prior", mode=EmailOutreachMode.COLD_OUTREACH,
            recipient_email="prior@acme.com", drafted_subject="s", drafted_body="b",
        )
        finalize_email_outreach_attempt(s, attempt, status=EmailOutreachStatus.SENT)

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())
    assert result.sent is False
    assert "Daily email cap reached" in result.aborted_reason
    assert draft_calls == []


def test_declining_final_confirmation_leaves_aborted_draft_on_record_sender_never_invoked(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake_sender = _patch_common(monkeypatch, draft_subject="Hello", draft_body="World")
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000305", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="a@acme.com",
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction(confirm=False))

    assert result.sent is False
    assert result.aborted_reason == "user declined final confirmation"
    assert fake_sender.sent_message is None

    with session_scope(factory) as s:
        attempt = s.query(EmailOutreachAttempt).one()
        assert attempt.status == EmailOutreachStatus.ABORTED
        assert attempt.drafted_subject == "Hello"
        assert attempt.drafted_body == "World"
        assert attempt.aborted_reason == "user declined final confirmation"


def test_edit_flips_human_edited(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_common(monkeypatch, draft_subject="Draft subject", draft_body="Draft body")
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000306", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="a@acme.com",
    )

    interaction = FakeInteraction(edited_subject="Edited subject")
    result = run_email_outreach_workflow(settings, str(job_id), interaction=interaction)

    assert result.sent is True
    assert result.human_edited is True
    with session_scope(factory) as s:
        attempt = s.query(EmailOutreachAttempt).one()
        assert attempt.final_subject == "Edited subject"
        assert attempt.human_edited is True


def test_accepting_draft_as_is_does_not_flag_human_edited(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_common(monkeypatch, draft_subject="Draft subject", draft_body="Draft body")
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000307", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="a@acme.com",
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())
    assert result.human_edited is False


def test_send_failure_finalizes_aborted_with_type_only_reason(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_common(monkeypatch, sender=FakeSender(fail=True))
    factory, job_id = _make_job_with_signal(
        settings, "x", "040926000308", signal=EmailApplicationSignal.CONTACT_ONLY,
        contact_email="a@acme.com",
    )

    result = run_email_outreach_workflow(settings, str(job_id), interaction=FakeInteraction())
    assert result.sent is False
    assert result.aborted_reason == "send failed: EmailSendError"  # type name only, never str(exc)
    assert "SMTPException" not in result.aborted_reason  # the underlying detail is never leaked

    with session_scope(factory) as s:
        attempt = s.query(EmailOutreachAttempt).one()
        assert attempt.status == EmailOutreachStatus.ABORTED


def test_unknown_job_aborts_cleanly(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    result = run_email_outreach_workflow(settings, "999999999999", interaction=FakeInteraction())
    assert result.sent is False
    assert "No job matched" in result.aborted_reason


def test_structural_guard_no_browser_imports_in_email_outreach_runner():
    """No actual import of Playwright/the Naukri browser client anywhere
    in this module -- sending an email needs no browser session at all.
    (The module's own docstring explains this in prose, which is why the
    check is against import statements specifically, not the raw source.)"""
    import inspect

    from naukri_agent.orchestration import email_outreach_runner

    src = inspect.getsource(email_outreach_runner)
    assert "browser_manager" not in src
    assert "naukri_client" not in src
    assert "import playwright" not in src.lower()


def test_structural_guard_email_outreach_runner_never_imported_by_unattended_paths():
    import inspect

    from naukri_agent.orchestration import discovery, pipeline
    from naukri_agent.scheduler import daemon

    for mod in (pipeline, discovery, daemon):
        src = inspect.getsource(mod)
        assert "email_outreach_runner" not in src
