"""Answer what Naukri's form really asks, one question at a time.

Found 23-26 Oct (Acme Analytics): Naukri's data listed 7 questions, but its form asked ONE ("How many years of experience do
you have in Deployment?") - it keeps the answers it already has and asks only what is left, and it fills notice period and pay
from the profile. The app asked all 7 on Telegram and typed answers into questions the form never asked, in a burst; one answer
was lost, the bot said "Sorry! I am not trained to answer this query :(" (saved panel, 10 Oct), and the application failed."""

from __future__ import annotations

import json

import pytest

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_workflow import ApplyAnswerError, ApplyWorkflowSession, FormChanged
from naukri_agent.browser.models import ApplyQuestionPrompt, LiveQuestion
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings
from naukri_agent.orchestration.auto_apply_runner import MAX_LIVE_QUESTIONS, PARKED_NEEDS_HUMAN
from naukri_agent.orchestration.telegram_interaction import TelegramInteraction

from .browser_fakes import FakePage
from .test_apply_replay import ReplayClient
from .test_auto_apply_interactive import FakeHuman, go
from .test_auto_apply_runner import _patch_profile, cfg, q, seed  # noqa: F401 - autouse fixture
from .test_telegram import World, button

# =====================================================================================================================
# the panel: reading the question on screen
# =====================================================================================================================


class _Tap:
    def __init__(self, text, page):
        self.text, self.page = text, page

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text

    def click(self):
        self.page.tapped.append(self.text)


class LivePage(FakePage):
    """The open form as a script of screens; pressing Save moves to the next one. A screen is None (no form) or
    {'bots': [...], 'options': [...], 'textVisible': bool}, the same shape the real page reports."""

    def __init__(self, screens, applied=False):
        super().__init__()
        self.screens, self.i, self.typed, self.tapped, self.applied = list(screens), 0, [], [], applied

    def _screen(self):
        return self.screens[min(self.i, len(self.screens) - 1)]

    def evaluate(self, script, arg=None):
        s = self._screen()
        if isinstance(arg, list):  # the cheap look: (form on screen?, how many bot messages)
            return [False, 0] if s is None else [True, len(s["bots"])]
        return s

    def query_selector_all(self, selector):
        s = self._screen()
        if selector == selectors.ALREADY_APPLIED_MARKER:
            return [_Tap("Applied", self)] if self.applied else []
        if selector != selectors.APPLY_CHOICE_LABEL or s is None:
            return []
        return [_Tap(o, self) for o in s["options"]]

    def fill(self, selector, value, **kw):
        self.typed.append(value)
        super().fill(selector, value, **kw)

    def click(self, selector, **kw):
        super().click(selector, **kw)
        if selector == selectors.APPLY_DRAWER_SAVE:
            self.i += 1


def text_screen(*bots):
    return {"bots": list(bots), "options": [], "textVisible": True}


def choice_screen(question, *options):
    return {"bots": [question], "options": list(options), "textVisible": False}


def session(*screens):
    page = LivePage(screens)
    return ApplyWorkflowSession(page), page


def test_a_typed_question_is_read_from_the_forms_last_bot_message():
    s, _ = session(text_screen("Hello", "How many years of experience do you have in Deployment?"))
    state, live = s.read_live_question()
    assert state == "question" and live == LiveQuestion(
        text="How many years of experience do you have in Deployment?", options=[], kind="text")


def test_a_choice_question_carries_the_choices_on_screen():
    s, _ = session(choice_screen("What is your notice period?", "15 Days or less", "30 Days"))
    _state, live = s.read_live_question()
    assert (live.kind, live.options) == ("choice", ["15 Days or less", "30 Days"])


def test_the_bots_sorry_message_is_not_mistaken_for_the_question():
    s, _ = session(text_screen("Years of Python?", "Sorry! I am not trained to answer this query :("))
    assert s.read_live_question()[1].text == "Years of Python?"
    s2, _ = session(text_screen("Please try again"))
    assert s2.read_live_question(wait_ms=600)[0] == "timeout"  # nothing but noise: no question to answer


def test_while_the_bot_is_still_typing_there_is_nothing_to_answer_yet():
    s, page = session({"bots": ["Q?"], "options": [], "textVisible": False})
    assert s.read_live_question(wait_ms=900)[0] == "timeout" and page.wait_for_timeout_calls


def test_a_form_left_on_screen_with_nothing_to_answer_but_naukri_showing_applied_is_closed_not_stuck():
    page = LivePage([{"bots": ["Thank you, your application has been sent"], "options": [], "textVisible": False}], applied=True)
    assert ApplyWorkflowSession(page).read_live_question(wait_ms=600)[0] == "closed"


def test_a_form_that_is_gone_is_closed_but_straight_after_apply_it_gets_longer_to_appear():
    s, page = session(None)
    assert s.read_live_question()[0] == "closed" and len(page.wait_for_timeout_calls) == 1  # two looks in a row
    s2, page2 = session(None)
    assert s2.read_live_question(first=True)[0] == "closed" and len(page2.wait_for_timeout_calls) == 15  # ~5 s grace


def test_a_new_message_is_never_paired_with_the_previous_questions_choices():
    # the bot's new message has arrived but the old choices are still drawn: only a question that is unchanged on two
    # looks in a row counts
    stale = {"bots": ["Q1?", "Q2?"], "options": ["Yes", "No"], "textVisible": False}
    settled = {"bots": ["Q1?", "Q2?"], "options": [], "textVisible": True}
    page = LivePage([stale, settled, settled])
    ticks = iter(range(100))
    page.wait_for_timeout = lambda ms: setattr(page, "i", next(ticks) + 1)  # time passes: the screen changes underneath
    state, live = ApplyWorkflowSession(page).read_live_question()
    assert state == "question" and (live.text, live.kind, live.options) == ("Q2?", "text", [])


def test_the_answer_is_typed_only_into_the_question_it_was_meant_for():
    s, page = session(text_screen("How many years of experience do you have in Deployment?"), None)
    live = LiveQuestion(text="How many years of experience do you have in Deployment?", kind="text")
    assert s.answer_live_question(live, "3") == "closed"
    assert page.typed == ["3"] and page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_if_the_form_moved_on_to_another_question_nothing_is_typed():
    s, page = session(text_screen("What is your notice period?"))
    asked = LiveQuestion(text="How many years of experience do you have in Deployment?", kind="text")
    with pytest.raises(FormChanged, match="no longer asking"):
        s.answer_live_question(asked, "3")
    assert page.typed == [] and page.clicked == []


def test_if_the_form_has_closed_nothing_is_typed():
    s, page = session(None)
    with pytest.raises(FormChanged, match="no longer open"):
        s.answer_live_question(LiveQuestion(text="Q?", kind="text"), "3")
    assert page.typed == [] and page.clicked == []


def test_a_choice_is_tapped_by_its_text_in_any_case_and_saved():
    s, page = session(choice_screen("Notice period?", "15 Days or less", "30 Days"), None)
    live = LiveQuestion(text="Notice period?", options=["15 Days or less", "30 Days"], kind="choice")
    assert s.answer_live_question(live, "  30 DAYS ") == "closed"
    assert page.tapped == ["30 Days"] and page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_a_choice_that_is_not_on_screen_taps_nothing():
    s, page = session(choice_screen("Notice period?", "15 Days or less", "30 Days"))
    live = LiveQuestion(text="Notice period?", options=["15 Days or less", "30 Days"], kind="choice")
    with pytest.raises(ApplyAnswerError, match="not one of the choices"):
        s.answer_live_question(live, "1 Month")
    assert page.tapped == [] and page.clicked == []


def test_a_changed_form_is_an_apply_answer_error_so_old_handlers_still_catch_it():
    assert issubclass(FormChanged, ApplyAnswerError)


def test_the_client_hands_both_calls_to_the_session():
    client = NaukriClient(FakePage(), Settings(_env_file=None))
    seen = []

    class Sess:
        def read_live_question(self, wait_ms, first):
            seen.append(("read", wait_ms, first))
            return "closed", None

        def answer_live_question(self, expected, answer):
            seen.append(("answer", answer))
            return "next"

    client._apply_session = Sess()
    assert client.read_live_question(5000, True) == ("closed", None) and client.answer_live_question(object(), "3") == "next"
    assert seen == [("read", 5000, True), ("answer", "3")]


# =====================================================================================================================
# the runner: ask only what the form asks
# =====================================================================================================================

QS7 = [q(t, f"q{i}") for i, t in enumerate([
    "How many years of experience do you have in Generative Ai?", "How many years of experience do you have in Python?",
    "How many years of experience do you have in Natural Language Processing?",
    "How many years of experience do you have in Deployment?", "What is your notice period?",
    "What is your current CTC in Lacs per annum?", "What is your expected CTC in Lacs per annum?"], 1)]
DEPLOY = LiveQuestion(text="How many years of experience do you have in Deployment?", kind="text")


class LiveClient(ReplayClient):
    def __init__(self, pages, script, statuses=(), errors=()):
        super().__init__(pages)
        self.script, self.live_statuses, self.errors = list(script), list(statuses), list(errors)
        self.live_typed, self.firsts = [], []

    def read_live_question(self, wait_ms=15000, first=False):
        self.events.append("read")
        self.firsts.append(first)
        return self.script.pop(0) if self.script else ("closed", None)

    def answer_live_question(self, expected, answer):
        self.events.append(("live-answer", expected.text, answer))
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.live_typed.append((expected.text, answer))
        return self.live_statuses.pop(0) if self.live_statuses else "next"


class LiveHuman(FakeHuman):
    def __init__(self, *a, confirm_answers=(True,), **kw):
        super().__init__(*a, **kw)
        self.confirm_answers, self.asked_full, self.answer_confirmations = list(confirm_answers), [], []

    def ask_question(self, index, total, text, suggestion, options=None):
        self.asked_full.append((index, total, text, suggestion, options))
        return super().ask_question(index, total, text, suggestion, options)

    def confirm_answer(self, question, answer):
        self.answer_confirmations.append((question, answer))
        return self.confirm_answers.pop(0) if self.confirm_answers else None


def one_listed(tmp_path, questions=QS7, **over):
    c = cfg(tmp_path, **over)
    factory, urls = seed(c, [("t", "040926009301", 90.0, {})])
    return c, factory, urls["t"], {"questions": list(questions)}


def question(text, kind="text", options=()):
    return ("question", LiveQuestion(text=text, options=list(options), kind=kind))


def test_seven_listed_but_one_asked_means_one_question_one_confirmation_one_answer(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text), ("closed", None)], statuses=["closed"])
    human = LiveHuman(approvals=[True], answers=["3"])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and r.outcomes[0].outcome == "applied"
    assert [(a[0], a[1], a[2]) for a in human.asked_full] == [(1, 0, DEPLOY.text)]  # "Question 1", no total
    assert human.answer_confirmations == [(DEPLOY.text, "3")]
    assert fake.live_typed == [(DEPLOY.text, "3")] and fake.answered == []  # the old type-everything path was not used
    assert r.outcomes[0].questions == [DEPLOY.text] and r.outcomes[0].answers == ["3"]
    assert "asked 1 of the 7 questions in its list" in r.outcomes[0].detail
    assert fake.firsts[0] is True  # the first look after Apply allows for the form being slow to appear


def test_the_evidence_file_says_what_naukri_listed_and_what_its_form_asked(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text)], statuses=["closed"])
    go(c, factory, fake, LiveHuman(approvals=[True], answers=["3"]))
    files = list((tmp_path / "insp" / "auto_apply").glob("*_form.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(data["listed_by_naukri"]) == 7 and [a["question"] for a in data["asked_by_the_form"]] == [DEPLOY.text]
    assert data["asked_by_the_form"][0]["answer"] == "3" and data["handed_back"] is None


def test_a_tap_choice_needs_no_confirmation_and_a_typed_answer_does(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    script = [question("What is your notice period?", "choice", ["15 Days or less", "30 Days"]), question(DEPLOY.text)]
    fake = LiveClient({url: page}, script, statuses=["next", "closed"])
    human = LiveHuman(approvals=[True], answers=["30 Days", "3"])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and [t[1:] for t in fake.live_typed] == [("30 Days",), ("3",)]
    assert human.answer_confirmations == [(DEPLOY.text, "3")]  # only the typed one
    assert human.asked_full[0][4] == ["15 Days or less", "30 Days"] and human.asked_full[1][4] is None
    assert [a[0] for a in human.asked_full] == [1, 2]


def test_a_typed_answer_that_is_not_confirmed_is_never_sent(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text)])
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=["3"], confirm_answers=[False]))
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and fake.live_typed == [] and r.applied == 0
    assert "not confirmed" in r.outcomes[0].detail


def test_no_answer_on_telegram_hands_the_job_back_without_typing(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text)])
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=[]))
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and fake.live_typed == []
    assert "no answer was given on Telegram" in r.outcomes[0].detail


def test_a_handback_after_some_answers_says_naukri_keeps_them(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    script = [question("What is your notice period?", "choice", ["30 Days"]), question(DEPLOY.text)]
    fake = LiveClient({url: page}, script, statuses=["next"])
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=["30 Days"]))  # then silence on the typed question
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and r.outcomes[0].answers == ["30 Days"]
    assert "kept by Naukri" in r.outcomes[0].detail


def test_a_question_the_form_keeps_asking_is_asked_once_more_then_handed_back(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    page["submitted"] = False  # Naukri's own page does not show the job as Applied
    fake = LiveClient({url: page}, [question(DEPLOY.text)] * 3)
    human = LiveHuman(approvals=[True], answers=["3", "3"], confirm_answers=[True, True])
    r = go(c, factory, fake, human)
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and "keeps asking" in r.outcomes[0].detail
    assert len(human.asked_full) == 2  # once, and once more after it was not accepted; never a third time


def test_a_form_that_closes_while_you_think_is_reopened_and_your_answer_is_reused(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text), question(DEPLOY.text)], statuses=["closed"],
                      errors=[FormChanged("Naukri's application form is no longer open"), None])
    human = LiveHuman(approvals=[True], answers=["3"])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and fake.reopened == [url]
    assert len(human.asked_full) == 1 and human.answer_confirmations == [(DEPLOY.text, "3")]  # not asked or confirmed twice
    assert fake.live_typed == [(DEPLOY.text, "3")] and fake.firsts == [True, True]
    assert any("reopening it" in n for n in human.notes) and "reopened while you were answering" in r.outcomes[0].detail


def test_a_reopened_form_that_asks_something_else_asks_you_about_that_instead(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    fake = LiveClient({url: page}, [question(DEPLOY.text), question("What is your notice period?", "choice", ["30 Days"])],
                      statuses=["closed"], errors=[FormChanged("closed")])
    human = LiveHuman(approvals=[True], answers=["3", "30 Days"])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and [a[2] for a in human.asked_full] == [DEPLOY.text, "What is your notice period?"]


def test_a_form_that_keeps_changing_is_handed_back_after_two_reopenings(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    page["submitted"] = False  # Naukri's own page does not show the job as Applied
    changed = FormChanged("Naukri is no longer asking that question")
    fake = LiveClient({url: page}, [question(DEPLOY.text)] * 4, errors=[changed] * 3)
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=["3"]))
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and len(fake.reopened) == 2 and fake.live_typed == []
    assert "changed while waiting" in r.outcomes[0].detail


def test_a_form_open_but_asking_nothing_it_can_answer_is_handed_back(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    page["submitted"] = False  # Naukri's own page does not show the job as Applied
    fake = LiveClient({url: page}, [("timeout", None)])
    human = LiveHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and human.asked_full == [] and fake.live_typed == []
    assert "not asking anything I can answer" in r.outcomes[0].detail


def test_a_form_that_asks_too_many_questions_is_handed_back(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    page["submitted"] = False  # Naukri's own page does not show the job as Applied
    script = [question(f"Question number {i}?") for i in range(MAX_LIVE_QUESTIONS + 3)]
    fake = LiveClient({url: page}, script)
    human = LiveHuman(approvals=[True], answers=[str(i) for i in range(40)], confirm_answers=[True] * 40)
    r = go(c, factory, fake, human)
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and f"more than {MAX_LIVE_QUESTIONS}" in r.outcomes[0].detail
    assert len(fake.live_typed) == MAX_LIVE_QUESTIONS


def test_a_form_that_stopped_asking_but_whose_job_shows_as_applied_is_recorded_as_applied_not_handed_back(tmp_path):
    c, factory, url, page = one_listed(tmp_path)  # Naukri's page says Applied (the fake's default)
    fake = LiveClient({url: page}, [question(DEPLOY.text), ("timeout", None)], statuses=["next"])
    human = LiveHuman(approvals=[True], answers=["3"])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and r.outcomes[0].outcome == "applied" and fake.live_typed == [(DEPLOY.text, "3")]


def test_a_handback_that_the_person_caused_is_not_second_guessed_by_a_page_check(tmp_path):
    c, factory, url, page = one_listed(tmp_path)  # the fake says Applied, but the person gave no answer: nothing was sent
    fake = LiveClient({url: page}, [question(DEPLOY.text)])
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=[]))
    assert r.outcomes[0].outcome == PARKED_NEEDS_HUMAN and fake.confirmed_urls == []


def test_when_the_form_asks_nothing_at_all_the_job_is_simply_checked_and_recorded(tmp_path):
    c, factory, url, page = one_listed(tmp_path)  # Naukri listed 7 but had every answer already
    fake = LiveClient({url: page}, [("closed", None)])
    human = LiveHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and human.asked_full == [] and fake.live_typed == []
    assert "asked 0 of the 7 questions" in r.outcomes[0].detail


def test_the_profile_answer_is_only_a_suggestion_and_only_when_it_is_one_of_the_choices(tmp_path):
    c, factory, url, page = one_listed(tmp_path)
    script = [question("What is your notice period?", "choice", ["Zebra", "Giraffe"])]
    fake = LiveClient({url: page}, script, statuses=["closed"])
    human = LiveHuman(approvals=[True], answers=["Zebra"])
    go(c, factory, fake, human)
    assert human.asked_full[0][3] is None  # nothing in the profile matches those choices, so nothing is suggested


def test_with_the_setting_off_the_old_way_is_used(tmp_path):
    c, factory, url, page = one_listed(tmp_path, questions=[q("What is your current CTC?", "q1")], apply_follow_live_form=False)
    fake = LiveClient({url: page}, [question("anything")])
    human = LiveHuman(approvals=[True], answers=["5.5 LPA"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and "read" not in fake.events and fake.answered[0][1:] == ("q1", "5.5 LPA")


def test_a_client_that_cannot_read_the_live_form_uses_the_old_way(tmp_path):
    from .test_auto_apply_runner import FakeClient

    c, factory, url, page = one_listed(tmp_path, questions=[q("What is your current CTC?", "q1")])
    fake = FakeClient({url: page})
    r = go(c, factory, fake, LiveHuman(approvals=[True], answers=["5.5 LPA"], confirms=[True]))
    assert r.applied == 1 and fake.answered[0][1:] == ("q1", "5.5 LPA")


def test_the_live_flow_is_for_questions_only_a_question_free_job_is_unchanged(tmp_path):
    c, factory, url, page = one_listed(tmp_path, questions=[])
    fake = LiveClient({url: page}, [question("never read")])
    r = go(c, factory, fake, LiveHuman(approvals=[True]))
    assert r.applied == 1 and "read" not in fake.events


# =====================================================================================================================
# Telegram wording
# =====================================================================================================================


def interaction(world):
    return TelegramInteraction(world.channel(), Settings(_env_file=None, telegram_answer_timeout_minutes=1))


def test_a_question_without_a_total_is_just_question_n():
    w = World([[], [button("opt:1")]])
    assert interaction(w).ask_question(1, 0, "Notice?", None, options=["A", "B"]) == "B"
    assert w.sent()[0]["text"].startswith("Question 1:\nNotice?") and "1/0" not in w.sent()[0]["text"]
    w2 = World([[], [__import__("tests.test_telegram", fromlist=["text"]).text("3")]])
    interaction(w2).ask_question(2, 0, "Years?", None)
    assert w2.sent()[0]["text"].startswith("Question 2:\nYears?")


def test_a_question_with_a_total_still_says_n_of_total():
    w = World([[], [button("opt:0")]])
    interaction(w).ask_question(1, 3, "Notice?", None, options=["A"])
    assert w.sent()[0]["text"].startswith("Question 1/3:")


def test_confirming_a_typed_answer_shows_the_question_and_the_answer_and_says_sending():
    w = World([[], [button("y", message_id=9)]])
    assert interaction(w).confirm_answer("Years in Deployment?", "3") is True
    body = w.sent()[0]["text"]
    assert "Send this answer to Naukri?" in body and "Q: Years in Deployment?" in body and "A: 3" in body
    edits = [p for m, p in w.calls if m == "editMessageReplyMarkup"]
    assert "Sending" in edits[0]["reply_markup"]["inline_keyboard"][0][0]["text"]


def test_the_setting_is_on_by_default():
    assert Settings(_env_file=None).apply_follow_live_form is True
