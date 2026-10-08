"""
orchestration/apply_runner.py: the Phase 14 apply-agent pipeline logic.
Browser/LLM are faked at their construction points (same mocked-browser
tier as the rest of this project) so these tests exercise the
orchestration logic — database writes, the batch-review gate, the
final-confirm gate — without any real Playwright/network/LLM call.
"""

from __future__ import annotations

from naukri_agent.browser.models import ApplyQuestionPrompt, ApplySubmissionResult
from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import ApplicationHistory, ApplicationQuestion
from naukri_agent.database.repositories import list_application_questions
from naukri_agent.orchestration.apply_runner import ApplyUserInteraction, run_apply_workflow
from naukri_agent.resume.models import MasterResume

from .digest_fakes import add_job, settings as _digest_settings


def _settings(tmp_path):
    return _digest_settings(tmp_path)


def _candidate() -> CandidateProfile:
    return CandidateProfile(full_name="Jane Doe", email="jane@example.com", phone="123")


def _resume() -> MasterResume:
    return MasterResume(professional_summary="Summary.")


class FakeApplyClient:
    """Stands in for NaukriClient's apply-write surface only — the real
    DOM mechanics are already covered by test_browser_apply_workflow.py."""

    def __init__(self, page, settings, *, questions, submit_result, apply_type="native"):
        self.apply_type = apply_type
        self.questions = list(questions)
        self.submit_result = submit_result
        self.skipped: list[str] = []
        self.answered: list[tuple[str, str]] = []
        self.apply_clicked = False
        self.logged_in = False
        self.submitted_called = False
        self.calls: list[str] = []
        self.opened_url: str | None = None

    def login(self):
        self.logged_in = True
        self.calls.append("login")
        return None

    def open_job_page(self, url):
        self.opened_url = url
        self.calls.append("open_job_page")

    def detect_apply_type(self):
        self.calls.append("detect_apply_type")
        return self.apply_type

    def click_apply(self):
        self.apply_clicked = True
        self.calls.append("click_apply")

    def list_questions(self):
        return self.questions

    def skip_question(self, control_id):
        self.skipped.append(control_id)

    def submit_answer(self, control_id, answer):
        self.answered.append((control_id, answer))

    def submit_application(self):
        self.submitted_called = True
        return self.submit_result


class FakeInteraction(ApplyUserInteraction):
    """Canned answers — accepts the LLM draft for every question
    (flips human_edited only when a test explicitly overrides)."""

    def __init__(self, final_answers=None, confirm: bool = True):
        super().__init__()
        self._final_answers = final_answers
        self.confirm_answers_called_with: list | None = None
        self._confirm = confirm

    def confirm_answers(self, drafts):
        self.confirm_answers_called_with = drafts
        if self._final_answers is not None:
            return self._final_answers
        return [drafted or "" for _q, drafted in drafts]

    def confirm_submission(self, summary: str) -> bool:
        return self._confirm


def _patch_common(monkeypatch, tmp_path, *, fake_client, drafted_answer="30 days"):
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        "naukri_agent.browser.browser_manager.BrowserManager",
        lambda settings, profile_dir_override=None: _FakeBrowserManager(),
    )
    monkeypatch.setattr(
        "naukri_agent.browser.naukri_client.NaukriClient",
        lambda page, settings: fake_client,
    )
    monkeypatch.setattr(
        "naukri_agent.candidate.models.load_candidate_profile", lambda path: _candidate()
    )
    monkeypatch.setattr(
        "naukri_agent.resume.models.load_master_resume", lambda path: _resume()
    )

    class _FakeProvider:
        provider_name = "fake"
        model = "fake-model"

    monkeypatch.setattr(
        "naukri_agent.llm.factory.get_apply_llm_provider", lambda settings: _FakeProvider()
    )

    if drafted_answer is None:
        monkeypatch.setattr(
            "naukri_agent.agents.apply_answer_agent.draft_application_answers",
            lambda provider, question_texts, *a, **k: [None] * len(question_texts),
        )
    else:
        from naukri_agent.agents.apply_answer_agent import ApplyAnswerDraft

        monkeypatch.setattr(
            "naukri_agent.agents.apply_answer_agent.draft_application_answers",
            lambda provider, question_texts, *a, **k: [
                ApplyAnswerDraft(answer=drafted_answer, reason="r") for _ in question_texts
            ],
        )

    return settings


class _FakeBrowserManager:
    def __init__(self):
        self.page = object()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _make_job(settings, slug, ext):
    factory = init_db(settings)
    with session_scope(factory) as s:
        job = add_job(s, slug=slug, ext=ext)
        job_id = job.id
    return factory, job_id


def test_job_page_is_opened_after_login_and_before_clicking_apply(tmp_path, monkeypatch):
    """Regression for the first live run: login() leaves the browser on the
    Naukri homepage, so click_apply() timed out because nothing navigated to
    the job's own page first."""
    from naukri_agent.database.models import Job

    fake_client = FakeApplyClient(
        None, None, questions=[], submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    factory, job_id = _make_job(settings, "nav", "040926000250")

    run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    with session_scope(factory) as s:
        expected_url = s.get(Job, job_id).url
    assert fake_client.opened_url == expected_url
    assert fake_client.calls[:4] == ["login", "open_job_page", "detect_apply_type", "click_apply"]


def test_company_site_listing_aborts_cleanly_without_clicking_anything(tmp_path, monkeypatch):
    from naukri_agent.database.models import ApplicationHistory, ApplicationQuestion

    fake_client = FakeApplyClient(
        None, None, questions=[], submit_result=ApplySubmissionResult(submitted=True),
        apply_type="company_site",
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    factory, job_id = _make_job(settings, "cs", "040926000260")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.submitted is False
    assert "company site" in result.aborted_reason.lower()
    assert "mark-applied" in result.aborted_reason
    assert fake_client.apply_clicked is False
    assert fake_client.answered == [] and fake_client.skipped == []
    assert fake_client.submitted_called is False
    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 0
        assert s.query(ApplicationQuestion).count() == 0


def test_page_with_no_apply_button_aborts_cleanly_without_clicking_anything(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None, questions=[], submit_result=ApplySubmissionResult(submitted=True),
        apply_type="none",
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    _factory, job_id = _make_job(settings, "nb", "040926000261")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.submitted is False
    assert "No Naukri Apply button" in result.aborted_reason
    assert fake_client.apply_clicked is False


def test_skippable_question_is_skipped_not_drafted(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Relocate?", skippable=True)],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    factory, job_id = _make_job(settings, "x", "040926000200")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert fake_client.skipped == ["q1"]
    assert fake_client.answered == []
    assert result.questions_skipped == 1
    assert result.questions_asked == 0

    with session_scope(factory) as s:
        rows = list_application_questions(s, job_id=job_id)
        assert len(rows) == 1
        assert rows[0].was_skipped is True
        # confirm=True (default) -> the attempt submits, so even a
        # skip-only attempt gets its question row backfilled.
        assert rows[0].application_id is not None


def test_batch_review_flips_human_edited_when_final_differs_from_draft(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Notice period?")],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client, drafted_answer="30 days")
    factory, job_id = _make_job(settings, "x", "040926000201")

    interaction = FakeInteraction(final_answers=["45 days"])  # human edited the draft
    result = run_apply_workflow(settings, str(job_id), interaction=interaction)

    assert fake_client.answered == [("q1", "45 days")]
    assert result.submitted is True
    assert result.application_id is not None

    with session_scope(factory) as s:
        rows = list_application_questions(s, job_id=job_id)
        assert rows[0].drafted_answer == "30 days"
        assert rows[0].final_answer == "45 days"
        assert rows[0].human_edited is True
        assert rows[0].llm_provider == "fake"


def test_accepting_draft_as_is_does_not_flag_human_edited(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Notice period?")],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client, drafted_answer="30 days")
    factory, job_id = _make_job(settings, "x", "040926000202")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())
    assert result.submitted is True

    with session_scope(factory) as s:
        rows = list_application_questions(s, job_id=job_id)
        assert rows[0].human_edited is False


def test_multiple_mandatory_questions_draft_in_a_single_batched_llm_call(tmp_path, monkeypatch):
    """Efficiency guard: N mandatory questions must cost exactly ONE call
    to draft_application_answers, not N separate calls each resending
    the same candidate/resume grounding facts."""
    fake_client = FakeApplyClient(
        None, None,
        questions=[
            ApplyQuestionPrompt(control_id="q1", question_text="Notice period?"),
            ApplyQuestionPrompt(control_id="q2", question_text="Expected salary?"),
            ApplyQuestionPrompt(control_id="q3", question_text="Relocate?", skippable=True),
        ],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    factory, job_id = _make_job(settings, "x", "040926000206")

    calls = []

    def _fake_batch(provider, question_texts, *a, **k):
        calls.append(list(question_texts))
        from naukri_agent.agents.apply_answer_agent import ApplyAnswerDraft

        return [ApplyAnswerDraft(answer=f"answer for {q}", reason="r") for q in question_texts]

    monkeypatch.setattr(
        "naukri_agent.agents.apply_answer_agent.draft_application_answers", _fake_batch
    )

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.questions_asked == 2
    assert result.questions_skipped == 1
    assert len(calls) == 1  # exactly one batched call
    assert calls[0] == ["Notice period?", "Expected salary?"]  # skippable one excluded
    assert fake_client.answered == [
        ("q1", "answer for Notice period?"),
        ("q2", "answer for Expected salary?"),
    ]


def test_declining_final_confirmation_submits_nothing_but_keeps_question_audit(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Notice period?")],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client, drafted_answer="30 days")
    factory, job_id = _make_job(settings, "x", "040926000203")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction(confirm=False))

    assert result.submitted is False
    assert result.aborted_reason == "user declined final confirmation"
    assert fake_client.submitted_called is False

    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 0
        rows = list_application_questions(s, job_id=job_id)
        assert len(rows) == 1  # audit trail survives the abort
        assert rows[0].application_id is None


def test_confirmed_submission_writes_application_history_and_backfills_questions(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Notice period?")],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client, drafted_answer="30 days")
    factory, job_id = _make_job(settings, "x", "040926000204")

    result = run_apply_workflow(settings, str(job_id), interaction=FakeInteraction())

    assert result.submitted is True
    with session_scope(factory) as s:
        history = s.query(ApplicationHistory).one()
        assert history.source == "agent_auto_apply"
        assert history.job_id == job_id
        rows = s.query(ApplicationQuestion).filter_by(job_id=job_id).all()
        assert all(r.application_id == history.id for r in rows)


def test_unknown_job_aborts_without_opening_a_browser(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(None, None, questions=[], submit_result=ApplySubmissionResult(submitted=True))
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    init_db(settings)  # tables exist, but no job row

    result = run_apply_workflow(settings, "999999999999", interaction=FakeInteraction())
    assert result.submitted is False
    assert "No job matched" in result.aborted_reason
    assert fake_client.apply_clicked is False


def test_no_mandatory_questions_still_requires_final_confirmation(tmp_path, monkeypatch):
    fake_client = FakeApplyClient(
        None, None,
        questions=[ApplyQuestionPrompt(control_id="q1", question_text="Relocate?", skippable=True)],
        submit_result=ApplySubmissionResult(submitted=True),
    )
    settings = _patch_common(monkeypatch, tmp_path, fake_client=fake_client)
    factory, job_id = _make_job(settings, "x", "040926000205")

    interaction = FakeInteraction(confirm=False)
    result = run_apply_workflow(settings, str(job_id), interaction=interaction)
    assert result.submitted is False  # the gate applies even with zero mandatory questions


def test_structural_guard_apply_runner_never_imported_by_unattended_paths():
    """apply_runner is interactive (requires a human at the final
    confirm) and must never be reachable from run-daily/discover or the
    scheduler — mirrors test_stage_a_cli.py's existing reverse guard."""
    import inspect

    from naukri_agent.orchestration import discovery, pipeline
    from naukri_agent.scheduler import daemon

    for mod in (pipeline, discovery, daemon):
        src = inspect.getsource(mod)
        assert "apply_runner" not in src
