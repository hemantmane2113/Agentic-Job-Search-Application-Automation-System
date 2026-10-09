"""
Phase 14 apply-write workflow (browser/apply_workflow.py). Not frozen
(unlike apply_inspection.py) — but MutatingRequestBlocker and
extract_application_ui are imported from there UNMODIFIED, and these
tests prove that reuse rather than re-implementing the guard.
"""

from __future__ import annotations

import json

import pytest

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_workflow import ApplyAnswerError, ApplyWorkflowSession, _parse_questionnaire
from naukri_agent.browser.models import ApplyQuestionPrompt

from .browser_fakes import FakeElement, FakePage

_APPLY_INIT_PATH = "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"


def test_click_apply_clicks_only_the_apply_selector():
    page = FakePage()
    page.set_element(selectors.APPLY_BUTTON, FakeElement())
    session = ApplyWorkflowSession(page)
    session.click_apply()
    assert page.clicked == [selectors.APPLY_BUTTON]


def test_click_apply_installs_the_guard_armed():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.click_apply()
    # The guard is reused from apply_inspection.py unmodified -- a
    # non-allowlisted mutating request must still be blocked.
    route = page.simulate_request("POST", "https://www.naukri.com/some/other/mutating/endpoint")
    assert route.action == "abort"


def test_allowlisted_apply_init_request_still_passes_through():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.click_apply()
    route = page.simulate_request("POST", f"https://www.naukri.com{_APPLY_INIT_PATH}")
    assert route.action == "continue"


def test_list_questions_returns_empty_before_any_response_observed():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.click_apply()
    assert session.list_questions() == []


def test_list_questions_parses_a_top_level_questionnaire_response():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.click_apply()
    body = json.dumps(
        {
            "questionnaire": [
                {"id": "q1", "questionText": "Notice period?"},
                {"id": "q2", "questionText": "Relocate?"},
            ],
            "skippableQuestions": ["q2"],
        }
    ).encode()
    page.simulate_response("POST", f"https://www.naukri.com{_APPLY_INIT_PATH}", body=body)
    questions = session.list_questions()
    assert questions == [
        ApplyQuestionPrompt(control_id="q1", question_text="Notice period?", skippable=False),
        ApplyQuestionPrompt(control_id="q2", question_text="Relocate?", skippable=True),
    ]


def test_list_questions_ignores_unrelated_responses():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.click_apply()
    page.simulate_response("GET", "https://www.naukri.com/some/analytics/ping", body=b"{}")
    assert session.list_questions() == []


def test_submit_answer_fills_the_drawers_text_box_and_presses_its_save():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.submit_answer("q1", "30 days")
    assert page.filled == {selectors.APPLY_DRAWER_TEXT_INPUT: "30 days"}
    assert page.clicked == [selectors.APPLY_DRAWER_SAVE]


def test_a_text_answer_falls_back_to_the_older_selector_when_the_first_finds_nothing():
    class Page(FakePage):
        def fill(self, selector, value, **kw):
            if selector == selectors.APPLY_DRAWER_TEXT_INPUT:
                raise TimeoutError("no such element")
            super().fill(selector, value, **kw)

    page = Page()
    ApplyWorkflowSession(page).submit_answer("q1", "30 days")
    assert page.filled == {selectors.APPLY_ANSWER_INPUT: "30 days"}


def test_a_text_answer_with_no_box_anywhere_fails_clearly_and_clicks_nothing():
    class Page(FakePage):
        def fill(self, selector, value, **kw):
            raise TimeoutError("Page.fill: Timeout")

    page = Page()
    with pytest.raises(ApplyAnswerError, match="no text box"):
        ApplyWorkflowSession(page).submit_answer("q1", "30 days")
    assert page.clicked == []


def test_the_drawer_html_is_read_for_diagnostics_and_never_raises():
    class Page(FakePage):
        def evaluate(self, script, arg=None):
            return "<div class='chatbot_Drawer'>q</div>"

    assert ApplyWorkflowSession(Page()).drawer_html() == "<div class='chatbot_Drawer'>q</div>"

    class Broken(FakePage):
        def evaluate(self, script, arg=None):
            raise RuntimeError("page closed")

    assert ApplyWorkflowSession(Broken()).drawer_html() is None


def test_skip_question_clicks_only_the_skip_control():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.skip_question("q2")
    assert page.clicked == [selectors.APPLY_SKIP_BUTTON]


def test_submit_application_confirmed_when_page_lands_on_myapply():
    """Live 2026-10-08: the final click navigated to /myapply/saveApply; the old
    code crashed waiting for that navigation and recorded nothing."""
    page = FakePage()
    page.url = "https://www.naukri.com/myapply/saveApply?strJobsarr=[1]"
    result = ApplyWorkflowSession(page).submit_application()
    assert page.clicked == [selectors.APPLY_FINAL_SUBMIT_BUTTON]
    assert result.submitted is True and "confirmed" in result.notes[0]


def test_submit_application_not_reported_as_submitted_without_any_confirmation():
    page = FakePage()
    page.url = "https://www.naukri.com/job-listings-x-1"
    result = ApplyWorkflowSession(page).submit_application()
    assert result.submitted is False
    assert "verify on Naukri" in result.notes[0]


def test_submit_application_click_failure_returns_unsubmitted_instead_of_raising():
    class _BrokenPage(FakePage):
        def click(self, selector, **kwargs):
            raise TimeoutError("boom")

    result = ApplyWorkflowSession(_BrokenPage()).submit_application()
    assert result.submitted is False and "check Naukri" in result.notes[0]


def test_parse_questionnaire_handles_nested_chatbot_response_shape():
    body = {
        "chatbotResponse": {
            "questionnaire": [{"id": "q1", "question": "Years of experience?"}],
        },
        "skippableQuestions": [],
    }
    prompts, notes = _parse_questionnaire(body)
    assert prompts == [ApplyQuestionPrompt(control_id="q1", question_text="Years of experience?")]
    assert any("chatbotResponse" in n for n in notes)


def test_parse_questionnaire_returns_empty_and_notes_on_unknown_shape():
    prompts, notes = _parse_questionnaire({"statusCode": 0, "flowType": "default"})
    assert prompts == []
    assert notes and "could not locate" in notes[0]


# --- detect_apply_type: read-only classification of a loaded job page ---


class _Handle:
    def __init__(self, visible):
        self._visible = visible

    def is_visible(self):
        return self._visible


class _DetectPage:
    """Only the two calls detect_apply_type is allowed to make."""

    def __init__(self, matches):
        self._matches = matches  # selector -> list[_Handle]

    def query_selector_all(self, selector):
        return self._matches.get(selector, [])


def test_detect_apply_type_native_company_site_and_none():
    from naukri_agent.browser import selectors
    from naukri_agent.browser.apply_workflow import detect_apply_type

    native = _DetectPage({selectors.APPLY_BUTTON: [_Handle(True), _Handle(False)]})
    company = _DetectPage({selectors.COMPANY_SITE_APPLY_BUTTON: [_Handle(True), _Handle(False)]})
    hidden_only = _DetectPage({selectors.APPLY_BUTTON: [_Handle(False)]})
    both = _DetectPage({
        selectors.APPLY_BUTTON: [_Handle(True)],
        selectors.COMPANY_SITE_APPLY_BUTTON: [_Handle(True)],
    })

    assert detect_apply_type(native) == "native"
    assert detect_apply_type(company) == "company_site"
    assert detect_apply_type(hidden_only) == "none"
    assert detect_apply_type(_DetectPage({})) == "none"
    assert detect_apply_type(both) == "native"


def test_detect_apply_type_treats_a_page_that_raises_as_none():
    from naukri_agent.browser.apply_workflow import detect_apply_type

    class _Broken:
        def query_selector_all(self, selector):
            raise RuntimeError("page closed")

    assert detect_apply_type(_Broken()) == "none"


def test_detect_apply_type_waits_for_a_button_that_is_drawn_late():
    """Live finding (job 358): the Apply button is absent right after the page
    loads and visible ~1s later; checking instantly wrongly reported 'none'."""
    from naukri_agent.browser import selectors
    from naukri_agent.browser.apply_workflow import detect_apply_type

    class _LatePage(_DetectPage):
        def __init__(self):
            super().__init__({})
            self.waited_for = None

        def wait_for_selector(self, selector, state=None, timeout=None):
            self.waited_for = (selector, state, timeout)
            self._matches = {selectors.APPLY_BUTTON: [_Handle(True)]}  # button appears

    page = _LatePage()
    assert detect_apply_type(page) == "native"
    assert page.waited_for[1] == "visible" and page.waited_for[2] > 0


def test_discovery_stores_unknown_not_none_when_no_apply_control_was_seen():
    from naukri_agent.browser.jobs import _known_apply_type
    from naukri_agent.browser import selectors

    assert _known_apply_type(_DetectPage({})) is None
    assert _known_apply_type(_DetectPage({selectors.APPLY_BUTTON: [_Handle(True)]})) == "native"
    assert _known_apply_type(_DetectPage({selectors.COMPANY_SITE_APPLY_BUTTON: [_Handle(True)]})) == "company_site"


# --- the "Applied" marker: for question-free jobs the Apply click itself submits ----


class _MarkerPage(FakePage):
    """FakePage whose Applied marker is visible now, or appears when waited for."""

    def __init__(self, visible_now: bool = False, appears_on_wait: bool = False):
        super().__init__()
        self._visible = visible_now
        self._appears = appears_on_wait

    def query_selector_all(self, selector):
        return [_Handle(True)] if (selector == selectors.ALREADY_APPLIED_MARKER and self._visible) else []

    def wait_for_selector(self, selector, state=None, timeout=None):
        if selector == selectors.ALREADY_APPLIED_MARKER and self._appears:
            self._visible = True
            return None
        raise TimeoutError("not found")


def test_applied_marker_already_showing_counts_as_submitted_without_clicking_anything_else():
    page = _MarkerPage(visible_now=True)
    result = ApplyWorkflowSession(page).submit_application()
    assert result.submitted is True and "Applied" in result.notes[0]
    assert page.clicked == []  # no hunt for a submit button that does not exist


def test_applied_marker_that_appears_a_moment_later_is_waited_for():
    page = _MarkerPage(appears_on_wait=True)
    result = ApplyWorkflowSession(page).submit_application()
    assert result.submitted is True and page.clicked == []


def test_without_the_marker_the_old_submit_click_path_is_still_used():
    page = _MarkerPage()
    page.url = "https://www.naukri.com/myapply/saveApply?x=1"
    result = ApplyWorkflowSession(page).submit_application()
    assert page.clicked == [selectors.APPLY_FINAL_SUBMIT_BUTTON] and result.submitted is True
