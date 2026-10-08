"""
Phase 14 apply-write workflow (browser/apply_workflow.py). Not frozen
(unlike apply_inspection.py) — but MutatingRequestBlocker and
extract_application_ui are imported from there UNMODIFIED, and these
tests prove that reuse rather than re-implementing the guard.
"""

from __future__ import annotations

import json

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_workflow import ApplyWorkflowSession, _parse_questionnaire
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


def test_submit_answer_fills_and_advances():
    page = FakePage()
    page.set_element(selectors.APPLY_ANSWER_INPUT, FakeElement())
    page.set_element(selectors.APPLY_NEXT_BUTTON, FakeElement())
    session = ApplyWorkflowSession(page)
    session.submit_answer("q1", "30 days")
    assert page.filled[selectors.APPLY_ANSWER_INPUT] == "30 days"
    assert selectors.APPLY_NEXT_BUTTON in page.clicked


def test_skip_question_clicks_only_the_skip_control():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    session.skip_question("q2")
    assert page.clicked == [selectors.APPLY_SKIP_BUTTON]


def test_submit_application_clicks_final_submit_and_returns_provisional_result():
    page = FakePage()
    session = ApplyWorkflowSession(page)
    result = session.submit_application()
    assert page.clicked == [selectors.APPLY_FINAL_SUBMIT_BUTTON]
    assert result.submitted is True
    assert result.notes  # flags it as provisional, never observed live


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
