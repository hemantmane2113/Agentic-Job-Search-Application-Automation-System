from naukri_agent.browser import jobs as jobs_module
from naukri_agent.browser import selectors

from .browser_fakes import FakeElement, FakePage


def _job_card(title: str, company: str, location: str, href: str) -> FakeElement:
    return FakeElement(
        children={
            selectors.JOB_CARD_TITLE: FakeElement(text=title, attrs={"href": href}),
            selectors.JOB_CARD_COMPANY: FakeElement(text=company),
            selectors.JOB_CARD_LOCATION: FakeElement(text=location),
        }
    )


# --- search_jobs ---


def test_search_jobs_parses_listing_cards():
    page = FakePage()
    page.set_element(
        selectors.JOB_CARD,
        [
            _job_card("Data Scientist", "Acme", "Pune", "https://naukri.com/job/1"),
            _job_card("ML Engineer", "Beta Corp", "Mumbai", "https://naukri.com/job/2"),
        ],
    )
    results = jobs_module.search_jobs(page, "data scientist")
    assert len(results) == 2
    assert results[0].title == "Data Scientist"
    assert results[0].company == "Acme"
    assert results[0].url == "https://naukri.com/job/1"
    assert results[1].title == "ML Engineer"


def test_search_jobs_no_results_returns_empty_list():
    page = FakePage()
    results = jobs_module.search_jobs(page, "a role that does not exist")
    assert results == []


def test_search_jobs_builds_url_from_query_and_location():
    page = FakePage()
    jobs_module.search_jobs(page, "Data Scientist", "Bangalore")
    assert page.goto_calls
    url = page.goto_calls[0]
    assert "data-scientist" in url
    assert "bangalore" in url


def test_search_jobs_never_calls_fill_or_click():
    page = FakePage()
    page.set_element(selectors.JOB_CARD, [_job_card("A", "B", "C", "u")])
    jobs_module.search_jobs(page, "data scientist")
    assert page.filled == {}
    assert page.clicked == []


# --- inspect_application_workflow ---


def test_detects_apply_button_present():
    page = FakePage()
    page.set_element(selectors.APPLY_BUTTON, FakeElement())
    result = jobs_module.inspect_application_workflow(page, "https://naukri.com/job/1")
    assert result.apply_button_present is True
    assert result.notes == []


def test_notes_missing_apply_button_rather_than_silently_reporting_false():
    page = FakePage()
    result = jobs_module.inspect_application_workflow(page, "https://naukri.com/job/1")
    assert result.apply_button_present is False
    assert len(result.notes) == 1


def test_detects_resume_selection_controls():
    page = FakePage()
    page.set_element(selectors.RESUME_SELECTION_CONTROLS, FakeElement())
    result = jobs_module.inspect_application_workflow(page, "https://naukri.com/job/1")
    assert result.resume_selection_controls_present is True


def test_inspect_workflow_never_clicks_apply():
    """The single most important read-only guarantee for this function."""
    page = FakePage()
    page.set_element(selectors.APPLY_BUTTON, FakeElement())
    jobs_module.inspect_application_workflow(page, "https://naukri.com/job/1")
    assert selectors.APPLY_BUTTON not in page.clicked
    assert page.clicked == []
