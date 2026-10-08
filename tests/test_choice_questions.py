"""Choice (radio) questions, end to end, built from the REAL apply-init response and
question panel captured live on 2026-10-08 (job 374, a Chennai walk-in question)."""

from __future__ import annotations

import pytest

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_type import question_panel_open
from naukri_agent.browser.apply_workflow import ApplyAnswerError, ApplyWorkflowSession, _parse_questionnaire
from naukri_agent.browser.models import ApplyQuestionPrompt
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationHistory, AutoApplyAttempt

from .browser_fakes import FakePage
from .test_auto_apply_interactive import FakeHuman, go
from .test_auto_apply_runner import FakeClient, _patch_profile, cfg, run, seed  # noqa: F401 - autouse fixture
from .test_telegram import JOB, World, button, interaction, text

# Structure copied from the live capture (long ids/URLs shortened).
REAL_BODY = {
    "statusCode": 0,
    "jobs": [
        {
            "jobId": "061026031503",
            "isCustom": False,
            "questionnaire": [
                {
                    "questionId": "51822330",
                    "questionName": "Will you be available for the Walk-in interview in Chennai on Oct 10 Saturday",
                    "questionType": "Radio Button",
                    "isMandatory": True,
                    "answerOption": {"newOption1": "Yes", "newOption2": "No"},
                    "questionnaireId": 12918289,
                }
            ],
            "companyName": "LatentView Analytics",
            "jobTitle": "Data Scientist",
        }
    ],
    "applyRedirectUrl": "https://www.naukri.com/myapply/saveApply?strJobsarr=[061026031503]&applytype=single",
    "chatbotResponse": {"options": [{"name": "Yes", "value": "Yes"}, {"name": "No", "value": "No"}]},
}


def test_parser_reads_the_real_questionnaire_shape_with_text_options_and_type():
    prompts, notes = _parse_questionnaire(REAL_BODY)
    assert len(prompts) == 1
    p = prompts[0]
    assert p.control_id == "51822330"
    assert p.question_text.startswith("Will you be available for the Walk-in interview in Chennai")
    assert p.options == ["Yes", "No"] and p.question_type == "Radio Button"
    assert p.skippable is False  # isMandatory is true
    assert any("jobs[].questionnaire" in n for n in notes)


def test_an_empty_questionnaire_is_reported_as_no_questions_not_as_a_parse_failure():
    body = {"jobs": [{"jobId": "1", "questionnaire": []}], "chatbotResponse": {}}
    prompts, notes = _parse_questionnaire(body)
    assert prompts == [] and any("no questions" in n for n in notes)
    assert not any("could not locate" in n for n in notes)


def test_a_non_mandatory_question_is_marked_skippable():
    body = {"jobs": [{"questionnaire": [{"questionId": "9", "questionName": "Anything else?", "isMandatory": False}]}]}
    assert _parse_questionnaire(body)[0][0].skippable is True


# --- answering in the panel ----------------------------------------------------------------


class _Label:
    def __init__(self, label, visible=True):
        self.label, self.visible, self.was_clicked = label, visible, False

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.label

    def click(self):
        self.was_clicked = True


class _PanelPage(FakePage):
    def __init__(self, labels):
        super().__init__()
        self.labels, self.waits = labels, 0

    def query_selector_all(self, selector):
        return list(self.labels) if selector == selectors.APPLY_CHOICE_LABEL else []

    def wait_for_timeout(self, ms):
        self.waits += 1


def _session(labels):
    page = _PanelPage(labels)
    session = ApplyWorkflowSession(page)
    session._capture.body = REAL_BODY
    assert session.list_questions()[0].options == ["Yes", "No"]
    return session, page


def test_the_chosen_option_is_clicked_then_the_panels_save_button():
    yes, no = _Label("Yes"), _Label("No")
    session, page = _session([yes, no])
    session.submit_answer("51822330", "No")
    assert no.was_clicked and not yes.was_clicked
    assert page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_an_answer_that_is_not_an_offered_option_clicks_nothing():
    yes, no = _Label("Yes"), _Label("No")
    session, page = _session([yes, no])
    with pytest.raises(ApplyAnswerError, match="not one of the offered options"):
        session.submit_answer("51822330", "Maybe")
    assert not yes.was_clicked and not no.was_clicked and page.clicked == []


def test_an_option_that_never_appears_raises_instead_of_hanging_or_guessing():
    session, page = _session([_Label("Yes", visible=False)])
    with pytest.raises(ApplyAnswerError, match="did not appear"):
        session.submit_answer("51822330", "Yes")
    assert page.clicked == [] and page.waits == 10


# --- is the panel open? ------------------------------------------------------------------------


class _Handle:
    def __init__(self, visible=True):
        self._v = visible

    def is_visible(self):
        return self._v


class _MarkerPage:
    def __init__(self, drawer, applied_on_wait=False):
        self.drawer, self.applied_on_wait, self.applied = drawer, applied_on_wait, False

    def query_selector_all(self, selector):
        if selector == selectors.APPLY_DRAWER:
            return [_Handle()] if self.drawer else []
        if selector == selectors.ALREADY_APPLIED_MARKER:
            return [_Handle()] if self.applied else []
        return []

    def wait_for_selector(self, selector, state=None, timeout=None):
        if selector == selectors.ALREADY_APPLIED_MARKER and self.applied_on_wait:
            self.applied = True
            return None
        raise TimeoutError("not found")


def test_question_panel_open_distinguishes_questions_from_a_question_free_submit():
    assert question_panel_open(_MarkerPage(drawer=True)) is True
    assert question_panel_open(_MarkerPage(drawer=False)) is False
    assert question_panel_open(_MarkerPage(drawer=True, applied_on_wait=True)) is False  # flashed panel, then Applied


# --- Telegram buttons --------------------------------------------------------------------------------


OPTS = ["Yes", "No"]


def test_choice_options_are_sent_as_buttons_and_a_tap_returns_that_option():
    w = World([[], [button("opt:1")]])
    assert w.channel().ask_choice("Walk-in?", OPTS, 60) == "No"
    keyboard = w.sent()[0]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard] == OPTS
    assert [row[0]["callback_data"] for row in keyboard] == ["opt:0", "opt:1"]


def test_typing_an_option_in_any_case_works_and_stop_is_passed_through():
    assert World([[], [text("yes")]]).channel().ask_choice("Q?", OPTS, 60) == "Yes"
    assert World([[], [text("/stop")]]).channel().ask_choice("Q?", OPTS, 60) == "/stop"


def test_choice_ignores_other_chats_unknown_text_stale_taps_and_silence():
    assert World([[], [text("yes", chat="999"), button("opt:0", chat="999")]]).channel().ask_choice("Q?", OPTS, 60) is None
    assert World([[], [text("banana")]]).channel().ask_choice("Q?", OPTS, 60) is None
    assert World([[button("opt:0")], [], []]).channel().ask_choice("Q?", OPTS, 60) is None  # old tap, drained
    assert World().channel().ask_choice("Q?", OPTS, 60) is None


def test_interaction_asks_choice_questions_with_buttons_and_shows_a_matching_suggestion():
    w = World([[], [button("opt:0")]])
    assert interaction(w).ask_question(1, 1, "Walk-in on Oct 10?", "Yes", options=OPTS) == "Yes"
    body = w.sent()[0]["text"]
    assert "Walk-in on Oct 10?" in body and "From your profile I'd answer: Yes" in body

    w2 = World([[], [button("opt:1")]])
    interaction(w2).ask_question(1, 1, "Q?", "Maybe", options=OPTS)  # suggestion not an option: not shown
    assert "From your profile" not in w2.sent()[0]["text"]


def test_interaction_choice_stop_and_silence_give_up():
    assert interaction(World([[], [text("/stop")]])).ask_question(1, 1, "Q?", None, options=OPTS) is None
    assert interaction(World()).ask_question(1, 1, "Q?", None, options=OPTS) is None


# --- the runner -----------------------------------------------------------------------------------------


def _walkin():
    return ApplyQuestionPrompt(
        control_id="51822330", question_text="Will you be available for the Walk-in interview in Chennai?",
        options=["Yes", "No"], question_type="Radio Button",
    )


def test_a_choice_question_goes_to_the_human_with_its_options_and_the_chosen_option_is_entered(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("walk", "040926003001", 90.0, {})])
    fake = FakeClient({urls["walk"]: {"questions": [_walkin()]}})
    human = FakeHuman(approvals=[True], answers=["Yes"], confirms=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1
    assert human.options_seen == [["Yes", "No"]] and human.suggestions == [None]
    assert fake.answered == [(urls["walk"], "51822330", "Yes")]


def test_unattended_never_types_a_profile_answer_that_is_not_one_of_the_offered_options(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("opt", "040926003101", 90.0, {})])
    notice = ApplyQuestionPrompt(control_id="q1", question_text="What is your notice period?", options=["Yes", "No"])
    fake = FakeClient({urls["opt"]: {"questions": [notice]}})
    r = run(c, factory, fake)  # profile would say "Immediate", which is not Yes/No
    assert r.outcomes[0].outcome == "needs_human" and "offered options" in r.outcomes[0].detail
    assert fake.answered == [] and fake.submitted_urls == []


@pytest.mark.parametrize("interactive", [False, True])
def test_an_open_question_panel_with_unreadable_questions_is_never_submitted(tmp_path, interactive):
    """The exact bug from the first real question screen: the parser found nothing, the
    old checks saw no input fields, and the app tried to submit an unanswered application."""
    c = cfg(tmp_path)
    factory, urls = seed(c, [("panel", "040926003201", 90.0, {})])
    fake = FakeClient({urls["panel"]: {"questions": [], "fields": 0, "panel": True}})
    r = go(c, factory, fake, FakeHuman()) if interactive else run(c, factory, fake)
    assert r.outcomes[0].outcome == "needs_human"
    assert "question panel opened" in r.outcomes[0].detail
    assert fake.submitted_urls == [] and fake.answered == []
    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 0
        assert s.query(AutoApplyAttempt).one().outcome == "needs_human"
