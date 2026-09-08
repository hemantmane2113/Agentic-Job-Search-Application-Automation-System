from naukri_agent.browser import profile as profile_module
from naukri_agent.browser import selectors

from .browser_fakes import FakeElement, FakePage


def test_resume_state_captures_filename_and_controls():
    page = FakePage()
    page.set_element(selectors.RESUME_FILENAME, FakeElement(text="MyResume.pdf"))
    page.set_element(selectors.RESUME_LAST_UPDATED, FakeElement(text="Updated 3 days ago"))
    page.set_element(selectors.RESUME_UPLOAD_BUTTON, FakeElement())

    result = profile_module.get_profile_resume(page)

    assert result.resume_filename == "MyResume.pdf"
    assert result.last_updated_text == "Updated 3 days ago"
    assert result.upload_control_present is True
    assert result.remove_control_present is False


def test_resume_state_handles_missing_elements_as_undetermined_not_absent():
    page = FakePage()
    result = profile_module.get_profile_resume(page)
    assert result.resume_filename is None
    assert result.upload_control_present is False
    assert result.remove_control_present is False


def test_navigates_to_profile_url():
    page = FakePage()
    profile_module.get_profile_resume(page)
    assert selectors.PROFILE_URL in page.goto_calls


def test_never_calls_fill_or_click():
    """Stage 1 is read-only -- profile inspection must never write anything."""
    page = FakePage()
    page.set_element(selectors.RESUME_UPLOAD_BUTTON, FakeElement())
    page.set_element(selectors.RESUME_REMOVE_BUTTON, FakeElement())
    profile_module.get_profile_resume(page)
    assert page.filled == {}
    assert page.clicked == []
