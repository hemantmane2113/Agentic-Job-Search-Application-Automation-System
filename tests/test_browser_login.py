import pytest

from naukri_agent.browser import login as login_module
from naukri_agent.browser import selectors
from naukri_agent.browser.exceptions import (
    NaukriCaptchaError,
    NaukriLoginError,
    NaukriMfaError,
    NaukriUnexpectedPageError,
)
from naukri_agent.browser.models import LoginStatus
from naukri_agent.config import Settings

from .browser_fakes import FakeElement, FakePage


def _settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, naukri_email="candidate@example.com", naukri_password="s3cret")
    defaults.update(overrides)
    return Settings(**defaults)


def test_missing_credentials_raises_before_any_navigation():
    page = FakePage()
    with pytest.raises(NaukriLoginError):
        login_module.login(page, _settings(naukri_email="", naukri_password=""))
    assert page.goto_calls == []  # never even tried to navigate


def test_successful_login_returns_success_and_fills_correct_fields():
    page = FakePage()

    def redirect_after_submit(selector: str) -> None:
        page.url = "https://www.naukri.com/mnjuser/homepage"

    page.on_click = redirect_after_submit

    result = login_module.login(page, _settings())
    assert result.status == LoginStatus.SUCCESS
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"
    assert page.filled[selectors.LOGIN_PASSWORD_INPUT] == "s3cret"
    assert page.clicked == [selectors.LOGIN_SUBMIT_BUTTON]


def test_credentials_never_hardcoded_always_from_settings():
    """
    Same login() call with different settings must fill different
    credentials -- proving they come from Settings, not a constant.
    """

    def redirect_after_submit(selector: str, target: FakePage) -> None:
        target.url = "https://www.naukri.com/mnjuser/homepage"

    page1 = FakePage()
    page1.on_click = lambda sel: redirect_after_submit(sel, page1)
    login_module.login(page1, _settings(naukri_email="a@x.com", naukri_password="pw1"))

    page2 = FakePage()
    page2.on_click = lambda sel: redirect_after_submit(sel, page2)
    login_module.login(page2, _settings(naukri_email="b@y.com", naukri_password="pw2"))

    assert page1.filled[selectors.LOGIN_EMAIL_INPUT] == "a@x.com"
    assert page2.filled[selectors.LOGIN_EMAIL_INPUT] == "b@y.com"


def test_captcha_present_before_submit_raises_and_never_fills_form():
    page = FakePage()
    page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())
    assert page.filled == {}  # never attempted to fill credentials into a CAPTCHA'd page


def test_captcha_appearing_after_submit_raises():
    page = FakePage()

    def reveal_captcha(selector: str) -> None:
        page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())

    page.on_click = reveal_captcha
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())


def test_mfa_appearing_after_submit_raises():
    page = FakePage()

    def reveal_mfa(selector: str) -> None:
        page.set_element(selectors.MFA_INDICATOR, FakeElement())

    page.on_click = reveal_mfa
    with pytest.raises(NaukriMfaError):
        login_module.login(page, _settings())


def test_unexpected_post_login_page_raises():
    page = FakePage()
    page.url = "https://www.naukri.com/some-error-or-changed-page"
    with pytest.raises(NaukriUnexpectedPageError):
        login_module.login(page, _settings())


def test_login_never_attempts_to_solve_captcha_or_mfa():
    """
    Structural check: login.py's source must never reference any
    captcha/OTP-solving mechanism (an OCR/solver library, a
    "solve_captcha"-style call) -- only detection-and-raise.
    """
    import inspect

    source = inspect.getsource(login_module)
    lowered = source.lower()
    for forbidden in ("solve_captcha", "captcha_solver", "2captcha", "anticaptcha", "ocr", "bypass"):
        assert forbidden not in lowered


# --- check_already_logged_in ---


def test_check_already_logged_in_returns_result_when_on_success_url():
    page = FakePage()
    page.url = "https://www.naukri.com/mnjuser/homepage"
    result = login_module.check_already_logged_in(page)
    assert result is not None
    assert result.status == LoginStatus.SUCCESS


def test_check_already_logged_in_returns_none_when_still_on_login_page():
    page = FakePage()
    page.url = selectors.LOGIN_URL
    assert login_module.check_already_logged_in(page) is None


def test_check_already_logged_in_never_navigates_fills_or_clicks():
    page = FakePage()
    page.url = "https://www.naukri.com/mnjuser/homepage"
    login_module.check_already_logged_in(page)
    assert page.goto_calls == []
    assert page.filled == {}
    assert page.clicked == []


# --- authenticated-session guard (fixes the persistent-context bug) ---


def test_fresh_logged_out_session_runs_normal_login_flow():
    """Scenario 1: no auth indicator anywhere -> full normal login."""
    page = FakePage()

    def redirect_after_submit(selector: str) -> None:
        page.url = "https://www.naukri.com/mnjuser/homepage"

    page.on_click = redirect_after_submit

    result = login_module.login(page, _settings())
    assert result.status == LoginStatus.SUCCESS
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"
    assert page.goto_calls == [selectors.LOGIN_URL]


def test_persistent_already_authenticated_session_never_fills_credentials():
    """
    Scenario 2: the auth indicator is present from the very start
    (persistent browser profile already had a valid session) -- login()
    must return success without touching the login form at all.
    """
    page = FakePage()
    page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert "persistent session" in result.message.lower()
    assert page.goto_calls == []  # never even navigated to the login page
    assert page.filled == {}
    assert page.clicked == []


def test_already_authenticated_after_goto_redirect_never_fills_credentials():
    """
    Scenario 3: the session wasn't detected as authenticated BEFORE
    goto(LOGIN_URL), but goto() itself redirects to an authenticated
    page (Naukri auto-redirecting an already-logged-in session away
    from the login form) -- login() must catch this immediately after
    the goto and never attempt to fill anything.
    """
    page = FakePage()

    def reveal_authenticated_on_goto(url: str) -> None:
        if url == selectors.LOGIN_URL:
            page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_goto = reveal_authenticated_on_goto

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert "redirected" in result.message.lower()
    assert page.goto_calls == [selectors.LOGIN_URL]  # goto happened once
    assert page.filled == {}  # but never filled anything afterward
    assert page.clicked == []


def test_check_already_logged_in_uses_dom_indicator_as_primary_signal():
    """
    The DOM-based auth indicator alone (no matching URL at all) must
    be enough for check_already_logged_in to report success -- a
    missing/different URL is never treated as proof of failure.
    """
    page = FakePage()
    page.url = "https://www.naukri.com/some/other/page/entirely"
    page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    result = login_module.check_already_logged_in(page)
    assert result is not None
    assert result.status == LoginStatus.SUCCESS


def test_is_authenticated_helper_directly():
    page = FakePage()
    assert login_module.is_authenticated(page) is False
    page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())
    assert login_module.is_authenticated(page) is True
