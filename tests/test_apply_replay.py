"""Slow answers must not break the application, a second resume must not be blocked, and a failure must leave evidence.

Found in the 22-26 Oct runs (Tiger Analytics failed three times, each differently):
  * 23 Oct: the form had been open 2 min 28 s while the person answered on Telegram; then four answers were typed in ONE
    second and Naukri showed "There was an error while processing your job application".
  * 26 Oct: the form had been open 4 min 40 s and had closed ("no text box for this question was found").
  * 21 Oct: the safety guard stays armed for the rest of a browser session after the first application and blocked the
    resume upload for the second job (filevalidation.naukri.com/file).
  * 24 Oct: a resume change could not be confirmed and nothing was saved to show why."""

from __future__ import annotations

import types
from contextlib import contextmanager

import pytest

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_workflow import ApplyAnswerError, ApplyWorkflowSession
from naukri_agent.browser.models import ApplyQuestionPrompt, ResumeUploadResult
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings
from naukri_agent.orchestration import auto_apply_runner as runner
from naukri_agent.orchestration.auto_apply_runner import PARKED_NEEDS_HUMAN, _replay_plan, run_auto_apply

from .browser_fakes import FakePage
from .test_auto_apply_interactive import FakeHuman, go
from .test_auto_apply_runner import NOW, FakeClient, _patch_profile, cfg, q, seed  # noqa: F401 - autouse fixture
from .test_choice_questions import _Label, _two_question_body
from .test_resume_per_job import ResumeClient, choose


# =====================================================================================================================
# answering at Naukri's pace
# =====================================================================================================================


class ScriptedPage(FakePage):
    """A page whose question drawer reports (on screen?, number of bot messages) from a script, one entry per look."""

    def __init__(self, states, labels=()):
        super().__init__()
        self.states, self.labels = list(states), list(labels)

    def evaluate(self, script, arg=None):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        return list(state)

    def query_selector_all(self, selector):
        return list(self.labels) if selector == selectors.APPLY_CHOICE_LABEL else []


def test_a_text_answer_waits_until_the_bot_asks_the_next_question():
    page = ScriptedPage([(True, 3), (True, 3), (True, 3), (True, 4)])
    status = ApplyWorkflowSession(page).submit_answer("q1", "3")
    assert status == "next"
    assert page.filled == {selectors.APPLY_DRAWER_TEXT_INPUT: "3"} and page.clicked == [selectors.APPLY_DRAWER_SAVE]
    assert len(page.wait_for_timeout_calls) == 2  # it looked three times and waited twice, not typed on at once


def test_four_text_answers_are_never_typed_in_the_same_second_again():
    # each answer is only followed by the next once the bot has put a new message in the drawer
    page = ScriptedPage([(True, 3), (True, 4), (True, 4), (True, 5), (True, 5), (True, 6), (True, 6), (True, 7)])
    session = ApplyWorkflowSession(page)
    out = [session.submit_answer(f"q{i}", str(i)) for i in range(4)]
    assert out == ["next"] * 4 and page.filled == {selectors.APPLY_DRAWER_TEXT_INPUT: "3"}
    assert page.clicked == [selectors.APPLY_DRAWER_SAVE] * 4


def test_a_form_that_has_gone_is_reported_as_closed_but_a_redraw_is_not():
    assert ApplyWorkflowSession(ScriptedPage([(True, 3), (False, 0), (False, 0)])).submit_answer("q", "1") == "closed"
    blip = ScriptedPage([(True, 3), (False, 0), (True, 3), (True, 4)])
    assert ApplyWorkflowSession(blip).submit_answer("q", "1") == "next"


def test_nothing_happening_ends_the_wait_after_about_twelve_seconds():
    page = ScriptedPage([(True, 3)])
    assert ApplyWorkflowSession(page).submit_answer("q", "1") == "timeout"
    assert len(page.wait_for_timeout_calls) == 40  # 40 x 300 ms


def test_a_page_that_cannot_be_read_never_hangs_or_raises():
    page = ScriptedPage([RuntimeError("page closed")])
    assert ApplyWorkflowSession(page).submit_answer("q", "1") == "closed"
    assert len(page.wait_for_timeout_calls) <= 2


def _choice_session(labels, states):
    page = ScriptedPage(states, labels)
    session = ApplyWorkflowSession(page)
    session._capture.body = _two_question_body()
    session.list_questions()
    return session, page


def test_a_choice_answer_also_waits_for_the_next_question_and_a_skipped_one_does_not():
    blr = _Label("Bengaluru, Karnataka")
    session, page = _choice_session([_Label("Hyderabad, Telangana"), blr, _Label("Skip this question")], [(True, 3), (True, 3), (True, 4)])
    assert session.submit_answer("12", "Bengaluru, Karnataka") == "next" and blr.was_clicked
    skipped_session, _ = _choice_session([_Label("Hyderabad, Telangana")], [(True, 3)])
    assert skipped_session.submit_answer("11", "15 Days or less") == "skipped"  # Naukri was asking something else


def test_the_bot_message_selector_is_the_verified_one():
    assert selectors.APPLY_BOT_ITEM == ".chatbot_Drawer li.botItem"


# =====================================================================================================================
# the client: reopen the form, and leave evidence
# =====================================================================================================================


def test_reopening_reloads_the_job_clicks_apply_again_and_reads_the_questions(monkeypatch):
    client = NaukriClient(FakePage(), Settings(_env_file=None))
    calls = []
    fresh = [ApplyQuestionPrompt(control_id="1", question_text="Q?")]
    monkeypatch.setattr(client, "prepare_next_application", lambda: calls.append("forget"))
    monkeypatch.setattr(client, "open_job_page", lambda url: calls.append(("open", url)))
    monkeypatch.setattr(client, "detect_apply_type", lambda: "native")
    monkeypatch.setattr(client, "click_apply", lambda: calls.append("click"))
    monkeypatch.setattr(client, "list_questions", lambda: calls.append("read") or fresh)
    assert client.reopen_application("https://x/job") == fresh
    assert calls == ["forget", ("open", "https://x/job"), "click", "read"]  # nothing submitted, nothing else touched


def test_reopening_a_job_that_no_longer_has_an_apply_button_clicks_nothing(monkeypatch):
    client = NaukriClient(FakePage(), Settings(_env_file=None))
    clicked = []
    monkeypatch.setattr(client, "prepare_next_application", lambda: None)
    monkeypatch.setattr(client, "open_job_page", lambda url: None)
    monkeypatch.setattr(client, "detect_apply_type", lambda: "company_site")
    monkeypatch.setattr(client, "click_apply", lambda: clicked.append(1))
    with pytest.raises(ApplyAnswerError, match="no longer shows a Naukri Apply button"):
        client.reopen_application("https://x/job")
    assert clicked == []


def test_a_failure_snapshot_of_a_page_without_a_drawer_now_keeps_the_page_html(tmp_path):
    page = FakePage()
    page.content_html = "<html><body>Apply button, no panel</body></html>"
    NaukriClient(page, Settings(_env_file=None)).save_failure_snapshot(tmp_path / "attempt_failure")
    assert (tmp_path / "attempt_failure.png").exists()
    assert "Apply button, no panel" in (tmp_path / "attempt_failure.html").read_text(encoding="utf-8")  # was a picture only


def test_a_page_snapshot_saves_picture_and_html(tmp_path):
    page = FakePage()
    page.content_html = "<html>profile page</html>"
    NaukriClient(page, Settings(_env_file=None)).save_page_snapshot(tmp_path / "x_resume")
    assert (tmp_path / "x_resume.png").exists() and "profile page" in (tmp_path / "x_resume.html").read_text(encoding="utf-8")


# =====================================================================================================================
# the runner: ask first, then answer fast
# =====================================================================================================================


class ReplayClient(FakeClient):
    def __init__(self, pages, statuses=()):
        super().__init__(pages)
        self.reopened, self.events, self.statuses = [], [], list(statuses)

    def click_apply(self):
        self.events.append("click")
        super().click_apply()

    def reopen_application(self, url):
        self.events.append("reopen")
        self.reopened.append(url)
        fresh = self._p().get("fresh")
        if isinstance(fresh, Exception):
            raise fresh
        return list(self._p().get("questions", []) if fresh is None else fresh)

    def submit_answer(self, control_id, answer):
        self.events.append(("answer", control_id, answer))
        super().submit_answer(control_id, answer)
        return self.statuses.pop(0) if self.statuses else "next"


class SlowHuman(FakeHuman):
    """A person who takes `seconds` to answer each question, on a clock the runner reads."""

    def __init__(self, clock, seconds, **kw):
        super().__init__(**kw)
        self.clock, self.seconds = clock, seconds

    def ask_question(self, index, total, text, suggestion, options=None):
        self.clock.t += self.seconds
        return super().ask_question(index, total, text, suggestion, options)


@pytest.fixture
def clock(monkeypatch):
    c = types.SimpleNamespace(t=1000.0)
    monkeypatch.setattr(runner, "_clock", lambda: c.t)
    return c


QS = [q("What is your current CTC?", "q1"), q("What is your notice period?", "q2")]


def one_job(tmp_path, pages_extra=None, **cfg_over):
    c = cfg(tmp_path, **cfg_over)
    factory, urls = seed(c, [("qa", "040926009001", 90.0, {})])
    page = {"questions": QS, **(pages_extra or {})}
    return c, factory, urls["qa"], page


def test_slow_answers_are_sent_into_a_freshly_reopened_form(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path)
    fake = ReplayClient({url: page})
    human = SlowHuman(clock, 40, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and fake.reopened == [url]
    assert fake.events == ["click", "reopen", ("answer", "q1", "5.5 LPA"), ("answer", "q2", "Immediate")]
    assert any("reopening it" in n for n in human.notes)  # the person is told what is happening
    assert "freshly reopened form" in r.outcomes[0].detail


def test_quick_answers_go_straight_into_the_form_that_is_already_open(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path)
    fake = ReplayClient({url: page})
    human = SlowHuman(clock, 5, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and fake.reopened == []
    assert fake.events == ["click", ("answer", "q1", "5.5 LPA"), ("answer", "q2", "Immediate")]


def test_the_reopening_can_be_switched_off(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path, apply_panel_max_wait_seconds=0)
    fake = ReplayClient({url: page})
    r = go(c, factory, fake, SlowHuman(clock, 500, approvals=[True], answers=["a", "b"], confirms=[True]))
    assert r.applied == 1 and fake.reopened == []


def test_a_reopened_form_that_asks_something_new_sends_nothing_and_hands_the_job_back(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path, {"fresh": [q("Do you hold a valid visa?", "z9")]})
    fake = ReplayClient({url: page})
    human = SlowHuman(clock, 40, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and r.applied == 0
    assert fake.answered == [] and fake.submitted_urls == []
    assert "different questions" in r.outcomes[0].detail and any(n.startswith("NOT submitted") for n in human.notes)


def test_a_reopened_form_with_fewer_questions_gets_only_those_answers(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path, {"fresh": [QS[1]]})
    fake = ReplayClient({url: page})
    human = SlowHuman(clock, 40, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and [a[1:] for a in fake.answered] == [("q2", "Immediate")]


def test_a_form_that_cannot_be_reopened_stops_the_run_and_submits_nothing(tmp_path, clock):
    # "submitted": False = Naukri's own page does not show the job as Applied, so the recovery check finds nothing sent
    c, factory, url, page = one_job(
        tmp_path, {"fresh": ApplyAnswerError("the job no longer shows a Naukri Apply button"), "submitted": False}
    )
    fake = ReplayClient({url: page})
    human = SlowHuman(clock, 40, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.outcomes[0].outcome == "failed" and r.applied == 0 and fake.answered == []
    assert "no longer shows a Naukri Apply button" in r.outcomes[0].detail and "freshly reopened" not in r.outcomes[0].detail


def test_naukri_finishing_the_form_early_stops_the_answers_and_says_what_was_not_asked(tmp_path, clock):
    c, factory, url, page = one_job(tmp_path)
    fake = ReplayClient({url: page}, statuses=["closed"])  # the form closed after the first answer
    human = SlowHuman(clock, 5, approvals=[True], answers=["5.5 LPA", "Immediate"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and [a[1:] for a in fake.answered] == [("q1", "5.5 LPA")]  # no error, no second answer typed
    assert "Naukri finished the form before asking: What is your notice period?" in r.outcomes[0].detail


def test_the_replay_matches_answers_to_questions_by_their_text():
    old = [q("What is your notice period?", "a"), q("Years of Python?", "b")]
    answers = ["15 days", "3"]
    fresh = [ApplyQuestionPrompt(control_id="x", question_text="  years   of PYTHON? "),
             ApplyQuestionPrompt(control_id="y", question_text="What is your notice period?", options=["15 Days", "30 Days"])]
    plan = _replay_plan(old, answers, fresh)
    assert [(p[0].control_id, p[1]) for p in plan] == [("x", "3"), ("y", "15 Days")]  # same words, other spelling of the option


def test_the_replay_gives_up_rather_than_guess():
    old, answers = [q("A?", "a")], ["yes"]
    assert _replay_plan(old, answers, [q("A?", "a"), q("Something new?", "b")]) is None
    assert _replay_plan(old, answers, [ApplyQuestionPrompt(control_id="a", question_text="A?", options=["No", "Maybe"])]) is None
    assert _replay_plan(old, answers, []) == []  # Naukri asks nothing now: nothing to send


# =====================================================================================================================
# the runner: a second resume needs a fresh browser, and a failed change leaves evidence
# =====================================================================================================================


def counting_opener(fake, log):
    @contextmanager
    def _open(_settings):
        log.append("open")
        try:
            yield fake
        finally:
            log.append("close")

    return _open


def run_two(tmp_path, resumes, fake=None):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926009101", 95.0, {}), ("b", "040926009102", 90.0, {})])
    for slug, resume in zip(("a", "b"), resumes):
        if resume:
            choose(factory, urls[slug], resume)
    fake = fake or ResumeClient({u: {} for u in urls.values()})
    log = []
    r = run_auto_apply(c, session_factory=factory, open_client=counting_opener(fake, log), now=NOW,
                       interaction=FakeHuman(approvals=[True, True]))
    return r, fake, log


def test_a_different_resume_after_an_application_gets_a_fresh_browser(tmp_path):
    r, fake, log = run_two(tmp_path, ["data_scientist", "ai_ml_engineer"])
    assert r.applied == 2
    assert [p.split("/")[-1] for p in fake.ensured] == ["data_scientist.pdf", "ai_ml_engineer.pdf"]
    assert log == ["open", "close", "open", "close"]  # the guard that blocked the upload died with the first browser


def test_the_same_resume_twice_keeps_one_browser(tmp_path):
    r, fake, log = run_two(tmp_path, ["data_scientist", "data_scientist"])
    assert r.applied == 2 and log == ["open", "close"]


def test_the_first_resume_change_needs_no_restart_because_nothing_has_been_opened_yet(tmp_path):
    r, fake, log = run_two(tmp_path, ["data_scientist", None])
    assert r.applied == 2 and log == ["open", "close"]


def test_a_job_without_a_matched_resume_still_arms_the_guard_so_the_next_change_restarts(tmp_path):
    r, fake, log = run_two(tmp_path, [None, "ai_ml_engineer"])
    assert r.applied == 2 and log == ["open", "close", "open", "close"]


class EvidenceClient(ResumeClient):
    def __init__(self, pages):
        super().__init__(pages, verified=False)
        self.snapshots = []

    def ensure_profile_resume(self, path):
        super().ensure_profile_resume(path)
        return ResumeUploadResult(verified=False, changed=True, before_filename="old_resume.pdf", note="upload not shown")

    def save_page_snapshot(self, prefix):
        self.snapshots.append(str(prefix))


def test_a_resume_change_that_cannot_be_confirmed_saves_a_snapshot_and_says_what_naukri_showed(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926009201", 95.0, {})])
    choose(factory, urls["a"], "data_scientist")
    fake = EvidenceClient({urls["a"]: {}})
    r = run_auto_apply(c, session_factory=factory, open_client=counting_opener(fake, []), now=NOW,
                       interaction=FakeHuman(approvals=[True]))
    assert r.applied == 0 and r.outcomes[0].outcome == "failed"
    assert "Naukri shows: old_resume.pdf; upload not shown" in r.outcomes[0].detail and "nothing was applied" in r.outcomes[0].detail
    assert len(fake.snapshots) == 1 and fake.snapshots[0].endswith("_resume")


def test_the_waiting_time_default_is_a_minute():
    assert Settings(_env_file=None).apply_panel_max_wait_seconds == 60
