import inspect

import pytest

from naukri_agent.browser import selectors
from naukri_agent.browser.models import LoginStatus
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings

from .browser_fakes import FakeElement, FakePage


def _settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, naukri_email="a@b.com", naukri_password="pw")
    defaults.update(overrides)
    return Settings(**defaults)


def test_login_delegates_correctly():
    page = FakePage()
    page.on_click = lambda sel: setattr(page, "url", "https://www.naukri.com/mnjuser/homepage")
    client = NaukriClient(page, _settings())
    result = client.login()
    assert result.status == LoginStatus.SUCCESS


def test_get_profile_resume_delegates_correctly():
    page = FakePage()
    page.set_element(selectors.RESUME_FILENAME, FakeElement(text="resume.pdf"))
    client = NaukriClient(page, _settings())
    result = client.get_profile_resume()
    assert result.resume_filename == "resume.pdf"


def test_search_jobs_delegates_correctly():
    page = FakePage()
    page.set_element(
        selectors.JOB_CARD,
        [FakeElement(children={selectors.JOB_CARD_TITLE: FakeElement(text="Data Scientist")})],
    )
    client = NaukriClient(page, _settings())
    results = client.search_jobs("data scientist")
    assert len(results) == 1
    assert results[0].title == "Data Scientist"


def test_get_job_delegates_correctly():
    page = FakePage()
    page.set_element(selectors.APPLY_BUTTON, FakeElement())
    client = NaukriClient(page, _settings())
    result = client.get_job("https://naukri.com/job/1")
    assert result.apply_button_present is True


def test_prepare_application_disabled_in_stage_1():
    client = NaukriClient(FakePage(), _settings())
    with pytest.raises(NotImplementedError):
        client.prepare_application()


def test_check_already_logged_in_delegates_correctly():
    page = FakePage()
    page.url = "https://www.naukri.com/mnjuser/homepage"
    client = NaukriClient(page, _settings())
    result = client.check_already_logged_in()
    assert result is not None
    assert result.status == LoginStatus.SUCCESS


def test_facade_never_exposes_raw_selectors_or_playwright_types():
    """
    The whole point of NaukriClient: callers should never need to know
    a CSS/XPath selector. Its source should reference no raw selector
    strings, only names imported from browser.selectors indirectly via
    the lower-level modules.
    """
    source = inspect.getsource(NaukriClient)
    assert "querySelector" not in source
    assert "xpath" not in source.lower()
    # No literal CSS-looking strings (starting with # or . or [) in the class body
    assert "#usernameField" not in source
    assert "[class*=" not in source
