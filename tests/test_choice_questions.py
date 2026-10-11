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


# --- the network guard must let the answer through (live finding, job 374) -------------------


def test_the_guard_allows_exactly_apply_init_and_the_chatbot_answer_post_and_nothing_else():
    """Live: the answer POST was blocked by the default-deny guard and Naukri's panel
    said "Something went wrong". Only these two exact (method, path) pairs may pass."""
    from naukri_agent.browser import apply_workflow as aw

    assert aw._APPLY_INIT_ALLOWLIST == frozenset({
        ("POST", "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"),
        ("POST", "/cloudgateway-chatbot/chatbot-services/botapi/v5/respond"),
    })
    # tracking, follow-company and every other mutation stays blocked
    assert ("POST", "/cloudgateway-mynaukri/jobseeker-follow-services/v0/companygroups/1/followers/self") not in aw._APPLY_INIT_ALLOWLIST
    assert ("GET", "/cloudgateway-chatbot/chatbot-services/botapi/v5/respond") not in aw._APPLY_INIT_ALLOWLIST


def test_the_session_arms_the_blocker_with_that_allowlist():
    from naukri_agent.browser import apply_workflow as aw

    session = ApplyWorkflowSession(FakePage())
    assert session._blocker._allow_exact == aw._APPLY_INIT_ALLOWLIST


# --- latency fixes (live run: 36s of dead time after the answer was accepted) ----------------------


class _ReloadPage(_MarkerPage):
    """The Applied marker shows now, only after the job page is reloaded, or never."""

    def __init__(self, applied_now=False, applied_after_reload=False, goto_raises=False):
        super().__init__(drawer=False)
        self.applied = applied_now
        self._after_reload, self._goto_raises = applied_after_reload, goto_raises
        self.goto_urls, self.clicked = [], []

    def click(self, selector, **kwargs):
        self.clicked.append(selector)

    def goto(self, url):
        self.goto_urls.append(url)
        if self._goto_raises:
            raise RuntimeError("net::ERR_ABORTED")
        if self._after_reload:
            self.applied = True


JOB_URL = "https://www.naukri.com/job-listings-x-1"


def test_choice_answer_is_confirmed_at_once_when_the_job_already_shows_applied():
    page = _ReloadPage(applied_now=True)
    result = ApplyWorkflowSession(page).confirm_applied_after_save(JOB_URL)
    assert result.submitted is True and page.goto_urls == [] and page.clicked == []


def test_choice_answer_is_confirmed_after_one_reload_of_the_job_page():
    page = _ReloadPage(applied_after_reload=True)
    result = ApplyWorkflowSession(page).confirm_applied_after_save(JOB_URL)
    assert result.submitted is True and page.goto_urls == [JOB_URL] and "reloading" in result.notes[0]
    assert page.clicked == []  # never hunts for a button that does not exist


def test_choice_answer_without_any_applied_proof_is_unsubmitted_not_assumed():
    page = _ReloadPage()
    result = ApplyWorkflowSession(page).confirm_applied_after_save(JOB_URL)
    assert result.submitted is False and "verify on Naukri" in result.notes[0]


def test_a_failed_reload_is_reported_not_raised():
    result = ApplyWorkflowSession(_ReloadPage(goto_raises=True)).confirm_applied_after_save(JOB_URL)
    assert result.submitted is False and "could not be reloaded" in result.notes[0]


def test_the_guessed_final_submit_click_no_longer_waits_30_seconds():
    class _Recorder(FakePage):
        def click(self, selector, **kwargs):
            self.kw = kwargs
            self.clicked.append(selector)

    page = _Recorder()
    ApplyWorkflowSession(page).submit_application()
    assert page.kw["timeout"] == 5000 and page.kw["no_wait_after"] is True


def test_choice_answers_are_confirmed_from_naukris_state_not_a_guessed_submit_button(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("choice", "040926004001", 90.0, {})])
    fake = FakeClient({urls["choice"]: {"questions": [_walkin()]}})
    r = go(c, factory, fake, FakeHuman(approvals=[True], answers=["Yes"], confirms=[True]))
    assert r.applied == 1
    assert fake.confirmed_urls == [urls["choice"]] and fake.guessed_submit_calls == 0


def test_a_question_free_job_is_also_confirmed_from_naukris_state_not_a_guessed_button(tmp_path):
    """Live (jobs 352, 321): the Apply click submits, but the Applied marker only shows after a
    reload, so the old look-without-reloading logged two finished applications as unconfirmed."""
    c = cfg(tmp_path)
    factory, urls = seed(c, [("free", "040926004101", 90.0, {})])
    fake = FakeClient({urls["free"]: {}})
    r = go(c, factory, fake, FakeHuman(approvals=[True]))
    assert r.applied == 1
    assert fake.confirmed_urls == [urls["free"]] and fake.guessed_submit_calls == 0


def test_an_unproven_choice_answer_stops_the_run_and_records_nothing_as_applied(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("u1", "040926004201", 95.0, {}), ("u2", "040926004202", 90.0, {})])
    fake = FakeClient({urls["u1"]: {"questions": [_walkin()], "submitted": False}, urls["u2"]: {}})
    r = go(c, factory, fake, FakeHuman(approvals=[True, True], answers=["Yes"], confirms=[True]))
    assert r.outcomes[0].outcome == "unconfirmed" and fake.opened == [urls["u1"]]
    with session_scope(factory) as s:
        assert s.query(ApplicationHistory).count() == 0


def test_the_phone_is_asked_before_the_browser_starts_and_before_each_page_opens(tmp_path):
    from contextlib import contextmanager

    from naukri_agent.orchestration.auto_apply_runner import run_auto_apply

    from .test_auto_apply_runner import NOW

    c = cfg(tmp_path)
    factory, urls = seed(c, [("first", "040926004301", 95.0, {}), ("second", "040926004302", 90.0, {})])
    order = []
    fake = FakeClient({u: {} for u in urls.values()})
    real_open = fake.open_job_page
    fake.open_job_page = lambda url: (order.append("page"), real_open(url))[1]

    class Spy(FakeHuman):
        def send_approval(self, job):
            order.append("prompt:" + job["title"])
            super().send_approval(job)

    @contextmanager
    def tracing_opener(_settings):
        order.append("browser")
        yield fake

    run_auto_apply(c, session_factory=factory, open_client=tracing_opener, now=NOW,
                   interaction=Spy(approvals=[True, True]))
    assert order == ["prompt:first role", "browser", "page", "prompt:second role", "page"]


def test_a_job_that_lost_its_apply_button_tells_the_phone_to_ignore_the_pending_question(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("gone", "040926004401", 90.0, {})])
    human = FakeHuman(approvals=[True])
    go(c, factory, FakeClient({urls["gone"]: {"type": "company_site"}}), human)
    assert any("no longer has a Naukri Apply button" in n and "ignore the question above" in n for n in human.notes)


def test_the_page_scan_for_input_fields_is_skipped_when_the_questions_were_already_read(tmp_path):
    class NoScan(FakeClient):
        def application_question_field_count(self):
            raise AssertionError("scanning for input fields is wasted time when questions were parsed")

    c = cfg(tmp_path)
    factory, urls = seed(c, [("scan", "040926004501", 90.0, {})])
    fake = NoScan({urls["scan"]: {"questions": [_walkin()]}})
    r = go(c, factory, fake, FakeHuman(approvals=[True], answers=["Yes"], confirms=[True]))
    assert r.applied == 1


def test_telegram_prompt_can_be_sent_now_and_read_later_and_an_early_tap_is_kept():
    w = World([[], [button("y")]])  # drain finds nothing; the tap arrives while we were busy
    ch = w.channel()
    ch.send_yes_no("Apply?")
    assert len(w.sent()) == 1  # the prompt is already out
    assert ch.wait_yes_no(60) is True


# --- no second confirmation for tap-only (choice) answers --------------------------------------------


def test_tapping_a_choice_answer_is_the_confirmation_and_no_second_prompt_is_sent(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("tap", "040926005001", 90.0, {})])
    fake = FakeClient({urls["tap"]: {"questions": [_walkin()]}})
    human = FakeHuman(approvals=[True], answers=["Yes"], confirms=[])  # a second prompt would get silence -> no_reply
    r = go(c, factory, fake, human)
    assert r.applied == 1 and human.confirmed_with == []
    assert fake.answered == [(urls["tap"], "51822330", "Yes")]


def test_the_applied_message_lists_the_answers_that_were_sent(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("msg", "040926005101", 90.0, {})])
    human = FakeHuman(approvals=[True], answers=["No"])
    go(c, factory, FakeClient({urls["msg"]: {"questions": [_walkin()]}}), human)
    applied = [n for n in human.notes if n.startswith("Applied:")]
    assert len(applied) == 1
    assert "Q: Will you be available for the Walk-in interview in Chennai?" in applied[0] and "A: No" in applied[0]


def test_typed_answers_and_mixed_questions_still_get_the_final_confirmation(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("typed", "040926005201", 95.0, {}), ("mixed", "040926005202", 90.0, {})])
    typed = ApplyQuestionPrompt(control_id="t1", question_text="What is your notice period?")  # no options: typed
    fake = FakeClient({urls["typed"]: {"questions": [typed]}, urls["mixed"]: {"questions": [_walkin(), typed]}})
    human = FakeHuman(approvals=[True, True], answers=["Immediate", "Yes", "Immediate"], confirms=[True, True])
    r = go(c, factory, fake, human)
    assert r.applied == 2 and len(human.confirmed_with) == 2  # both were asked to confirm


def test_a_declined_typed_confirmation_still_blocks_the_submit(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("no", "040926005301", 90.0, {})])
    typed = ApplyQuestionPrompt(control_id="t1", question_text="What is your notice period?")
    fake = FakeClient({urls["no"]: {"questions": [typed]}})
    r = go(c, factory, fake, FakeHuman(approvals=[True], answers=["Immediate"], confirms=[False]))
    assert r.outcomes[0].outcome == "declined" and fake.answered == [] and fake.submitted_urls == []


def test_the_choice_prompt_says_a_tap_submits_immediately():
    w = World([[], [button("opt:0")]])
    interaction(w).ask_question(1, 1, "Walk-in?", None, options=["Yes", "No"])
    assert "submitted straight away" in w.sent()[0]["text"] and "no second confirmation" in w.sent()[0]["text"]


# --- --max-jobs: limit a test run to a few jobs ----------------------------------------------------------


def test_max_attempts_limits_how_many_jobs_are_offered_in_one_run(tmp_path):
    from contextlib import contextmanager

    from naukri_agent.orchestration.auto_apply_runner import run_auto_apply

    from .test_auto_apply_runner import NOW

    c = cfg(tmp_path, auto_apply_daily_cap=6)
    factory, urls = seed(c, [(f"m{i}", f"04092600600{i}", 95.0 - i, {}) for i in range(4)])
    fake = FakeClient({u: {} for u in urls.values()})

    @contextmanager
    def opener(_s):
        yield fake

    human = FakeHuman(approvals=[True] * 4)
    r = run_auto_apply(c, session_factory=factory, open_client=opener, now=NOW, interaction=human, max_attempts=2)
    assert r.applied == 2 and len(fake.opened) == 2 and len(human.asked) == 2


def test_the_cli_passes_max_jobs_through(monkeypatch):
    from click.testing import CliRunner

    import naukri_agent.cli.main as cli_main
    from naukri_agent.config import Settings
    from naukri_agent.orchestration.auto_apply_runner import AutoApplyRunResult

    seen = {}

    class _Quiet:
        def notify(self, text):
            pass

    def fake_run(settings, **kw):
        seen.update(kw)
        return AutoApplyRunResult()

    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1"))
    monkeypatch.setattr("naukri_agent.orchestration.auto_apply_runner.run_auto_apply", fake_run)
    monkeypatch.setattr("naukri_agent.orchestration.telegram_interaction.build_telegram_interaction", lambda s, channel=None: _Quiet())
    assert CliRunner().invoke(cli_main.cli, ["telegram-apply", "--max-jobs", "3"]).exit_code == 0
    assert seen["max_attempts"] == 3
    assert CliRunner().invoke(cli_main.cli, ["telegram-apply", "--max-jobs", "0"]).exit_code != 0  # must be >= 1


# --- checkbox questions and a question Naukri skipped (live 2026-10-10, Indium Software) ----------------------------------------


def _two_question_body():
    def q(qid, name, options):
        return {"questionId": qid, "questionName": name, "questionType": "Radio Button", "isMandatory": True,
                "answerOption": {f"newOption{i}": o for i, o in enumerate(options, 1)}}

    return {"jobs": [{"questionnaire": [
        q("11", "What is your notice period?", ["15 Days or less", "30 Days", "More than 30 days"]),
        q("12", "Please select the city you are currently residing or willing to relocate to",
          ["Hyderabad, Telangana", "Bengaluru, Karnataka", "Skip this question"]),
    ]}]}


def _two_question_session(labels):
    page = _PanelPage(labels)
    session = ApplyWorkflowSession(page)
    session._capture.body = _two_question_body()
    assert [p.control_id for p in session.list_questions()] == ["11", "12"]
    return session, page


def test_the_choice_selector_covers_both_radio_and_checkbox_options():
    assert "label.ssrc__label" in selectors.APPLY_CHOICE_LABEL and "label.mcc__label" in selectors.APPLY_CHOICE_LABEL


def test_a_checkbox_option_is_clicked_then_save():
    hyd, blr, skip = _Label("Hyderabad, Telangana"), _Label("Bengaluru, Karnataka"), _Label("Skip this question")
    session, page = _two_question_session([hyd, blr, skip])
    session.submit_answer("12", "Hyderabad, Telangana")
    assert hyd.was_clicked and not blr.was_clicked and not skip.was_clicked
    assert page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_a_question_naukri_skipped_is_noted_and_the_next_one_is_still_answered():
    hyd, blr, skip = _Label("Hyderabad, Telangana"), _Label("Bengaluru, Karnataka"), _Label("Skip this question")
    session, page = _two_question_session([hyd, blr, skip])  # the panel is asking the city, not the notice period
    session.submit_answer("11", "15 Days or less")  # must not raise, must not click anything
    assert page.clicked == [] and not any(h.was_clicked for h in (hyd, blr, skip))
    assert session.skipped_question_texts() == ["What is your notice period?"]
    session.submit_answer("12", "Bengaluru, Karnataka")
    assert blr.was_clicked and page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_an_option_missing_from_its_own_question_is_still_an_error_not_a_skip():
    # the panel shows the notice-period question, but with different wording for the option we want: do not skip it
    session, page = _two_question_session([_Label("30 Days"), _Label("More than 30 days")])
    with pytest.raises(ApplyAnswerError, match="did not appear"):
        session.submit_answer("11", "15 Days or less")
    assert session.skipped_question_texts() == [] and page.clicked == []


def test_options_that_match_nothing_we_know_are_an_error_not_a_skip():
    session, _page = _two_question_session([_Label("Something unrelated"), _Label("Another")])
    with pytest.raises(ApplyAnswerError, match="did not appear"):
        session.submit_answer("11", "15 Days or less")
    assert session.skipped_question_texts() == []


def test_extra_spaces_and_case_in_the_panels_label_still_match():
    odd = _Label("  15  days OR less ")
    session, page = _two_question_session([odd, _Label("30 Days")])
    session.submit_answer("11", "15 Days or less")
    assert odd.was_clicked and page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_a_new_application_forgets_the_previous_ones_skipped_questions():
    session, _ = _two_question_session([_Label("Hyderabad, Telangana")])
    session.submit_answer("11", "15 Days or less")
    assert session.skipped
    session.reset_capture()
    assert session.skipped == []
