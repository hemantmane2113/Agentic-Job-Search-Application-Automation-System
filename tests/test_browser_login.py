import logging
import tempfile
from pathlib import Path

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

# login()'s post-goto diagnostic capture writes any snapshot under
# settings.inspection_output_dir; keep every test's default off the repo.
_DIAG_DIR = Path(tempfile.mkdtemp(prefix="naukri_login_tests_"))


def _settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        naukri_email="candidate@example.com",
        naukri_password="s3cret",
        inspection_output_dir=_DIAG_DIR,
    )
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


# --- regression: static invisible reCAPTCHA container is NOT a challenge ---
#
# Naukri's normal login page always ships
#   <div class="g-recaptcha" data-sitekey="..." data-size="invisible">
# even when no CAPTCHA is presented. The old CAPTCHA_INDICATOR
# ("... , .g-recaptcha , ...") matched it, raising a false-positive
# NaukriCaptchaError on every fresh-profile login. See
# inspection_output/apply_20260909_115708/challenge_detected.html.


def _redirect_home(page: FakePage):
    def _cb(_selector: str) -> None:
        page.url = "https://www.naukri.com/mnjuser/homepage"
    return _cb


def test_static_invisible_recaptcha_container_is_not_a_captcha_challenge():
    page = FakePage()
    page.set_element(
        selectors.RECAPTCHA_STATIC_CONTAINER,
        FakeElement(attrs={"class": "g-recaptcha", "data-size": "invisible"}),
    )
    assert login_module._captcha_challenge_present(page) is False


def test_login_proceeds_when_only_the_static_invisible_recaptcha_is_present():
    page = FakePage()
    page.set_element(
        selectors.RECAPTCHA_STATIC_CONTAINER,
        FakeElement(attrs={"class": "g-recaptcha", "data-size": "invisible"}),
    )
    page.on_click = _redirect_home(page)

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"
    assert page.clicked == [selectors.LOGIN_SUBMIT_BUTTON]


def test_captcha_indicator_selector_has_no_bare_g_recaptcha_term():
    sel = selectors.CAPTCHA_INDICATOR
    assert ".g-recaptcha" in sel  # still referenced...
    # ...but only in a qualified form (never as a standalone term)
    import re

    for m in re.finditer(r"\.g-recaptcha", sel):
        after = sel[m.end():m.end() + 1]
        assert after in (":", "["), f"bare .g-recaptcha term at {m.start()} in {sel!r}"
    # genuine challenge-iframe indicator is kept (requirement 6)
    assert "iframe[src*='captcha']" in sel


def test_genuine_captcha_indicator_match_still_raises_before_submit():
    page = FakePage()
    page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())  # a challenge indicator matched
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())
    assert page.filled == {}


def test_visible_non_invisible_recaptcha_widget_is_treated_as_a_challenge():
    page = FakePage()
    # a .g-recaptcha rendered as a real widget (data-size not "invisible")
    page.set_element(
        selectors.RECAPTCHA_STATIC_CONTAINER,
        FakeElement(attrs={"class": "g-recaptcha", "data-size": "normal"}),
    )
    assert login_module._captcha_challenge_present(page) is True
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())


def test_recaptcha_container_missing_data_size_is_treated_as_a_challenge():
    page = FakePage()
    page.set_element(
        selectors.RECAPTCHA_STATIC_CONTAINER,
        FakeElement(attrs={"class": "g-recaptcha"}),  # no data-size at all
    )
    assert login_module._captcha_challenge_present(page) is True


def test_mfa_only_present_is_not_reported_as_a_captcha_challenge():
    page = FakePage()
    page.set_element(selectors.MFA_INDICATOR, FakeElement())
    assert login_module._captcha_challenge_present(page) is False


def test_mfa_detection_selector_and_flow_unchanged():
    # selector string is exactly as before this fix
    assert selectors.MFA_INDICATOR == "input[name*='otp'], input[placeholder*='OTP']"
    page = FakePage()
    page.on_click = lambda _sel: page.set_element(selectors.MFA_INDICATOR, FakeElement())
    with pytest.raises(NaukriMfaError):
        login_module.login(page, _settings())


def test_human_in_the_loop_pause_intact_for_a_genuine_captcha(tmp_path, monkeypatch):
    """A real CAPTCHA still routes through inspection._handle_login_challenge:
    browser stays open, terminal waits, then the run resumes on Enter."""
    from .test_browser_inspection import (  # reuse the existing harness
        FakeBrowserManager,
        _patch_browser_manager,
        _settings as _insp_settings,
    )
    from naukri_agent.browser.inspection import run_inspection

    manager = FakeBrowserManager(_insp_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    seen = {}

    def fake_wait(prompt: str) -> None:
        seen["closed_during_pause"] = manager.closed
        assert "CAPTCHA" in prompt
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        manager.page.url = "https://www.naukri.com/mnjuser/homepage"
        manager.page.set_element(selectors.JOB_CARD, [])

    report = run_inspection(_insp_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert seen["closed_during_pause"] is False  # browser open while waiting
    assert report["completed"] is True


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


# --- regression: login-phase 30s TimeoutError on a fresh (isolated-profile) form login ---
#
# First isolated-profile `inspect-apply` run: the login POST to
# /central-login-services/v1/login succeeded, the browser reached the
# authenticated /mnjuser/homepage, but login() sat on
# wait_for_load_state("networkidle") — never reached on Naukri's
# authenticated SPA — until its 30s Playwright timeout, raising
# TimeoutError before the auth checks below it could run.


def test_form_login_success_does_not_wait_on_networkidle():
    page = FakePage()

    def redirect_after_submit(selector: str) -> None:
        # what the real login POST + redirect produces
        page.url = "https://www.naukri.com/mnjuser/homepage"

    page.on_click = redirect_after_submit

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert page.clicked == [selectors.LOGIN_SUBMIT_BUTTON]
    states_waited = [state for state, _timeout in page.load_state_calls]
    # the unreliable state is never used
    assert "networkidle" not in states_waited
    # the settle that IS used is bounded (explicit timeout on every wait)
    assert states_waited, "expected a bounded post-submit settle wait"
    assert all(timeout is not None for _state, timeout in page.load_state_calls)


def test_form_login_success_recognized_via_verified_dom_indicator_after_submit():
    """The VERIFIED nav-avatar indicator present after submit is enough
    on its own — even with a non-/mnjuser URL."""
    page = FakePage()

    def authed_after_submit(selector: str) -> None:
        page.url = "https://www.naukri.com/some/spa/route"  # deliberately NOT /mnjuser
        page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_click = authed_after_submit

    result = login_module.login(page, _settings())
    assert result.status == LoginStatus.SUCCESS


def test_form_login_settle_is_non_fatal_when_every_load_state_wait_raises():
    """A page that never reaches the requested load state must not turn
    a successful login into a propagated TimeoutError — the settle is
    swallowed and the auth checks decide the outcome."""
    page = FakePage()
    page.wait_for_load_state_error = RuntimeError("Timeout 15000ms exceeded")

    def redirect_after_submit(selector: str) -> None:
        page.url = "https://www.naukri.com/mnjuser/homepage"

    page.on_click = redirect_after_submit

    result = login_module.login(page, _settings())  # must NOT raise
    assert result.status == LoginStatus.SUCCESS


def test_form_login_post_submit_captcha_and_mfa_still_detected_after_settle_change():
    page = FakePage()
    page.on_click = lambda sel: page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())

    page2 = FakePage()
    page2.on_click = lambda sel: page2.set_element(selectors.MFA_INDICATOR, FakeElement())
    with pytest.raises(NaukriMfaError):
        login_module.login(page2, _settings())


# --- regression: post-CAPTCHA recheck must survive a dead browser connection ---


def test_check_already_logged_in_survives_dead_connection():
    """
    If the user closes the browser window during a manual CAPTCHA, the
    subsequent auth recheck must return None (cannot confirm) rather
    than propagating "Connection closed while reading from the driver"
    from an unguarded page.url read.
    """
    page = FakePage()
    page.simulate_connection_loss()

    assert login_module.check_already_logged_in(page) is None


def test_safe_current_url_returns_none_when_url_raises():
    page = FakePage()
    page.simulate_connection_loss()
    assert login_module._safe_current_url(page) is None
    # and a live page still reads normally
    live = FakePage()
    live.url = "https://www.naukri.com/mnjuser/homepage"
    assert login_module._safe_current_url(live) == "https://www.naukri.com/mnjuser/homepage"


# --- regression: post-submit state read before the redirect has landed ---
#
# Naukri's login submit is an XHR POST (no document nav) followed by a JS
# redirect to /mnjuser/homepage. wait_for_load_state() only observes the
# already-loaded /nlogin/login document, so login() used to read a STALE
# page.url == /nlogin/login and a not-yet-present auth avatar, then raise
# a false NaukriUnexpectedPageError — even though the network log shows
# the login POST landing on /mnjuser/homepage a moment later.
# _settle_after_login_submit() now waits (bounded, no sleep) for the
# authenticated-nav indicator / a challenge to actually appear.

_LOGIN_URL = "https://www.naukri.com/nlogin/login"
_HOME_URL = "https://www.naukri.com/mnjuser/homepage"


def test_post_submit_stale_login_url_still_resolves_to_success_after_redirect():
    page = FakePage()
    # The login page genuinely shows its email field, so the post-goto
    # authenticated-session settle resolves immediately (no redirect) and
    # the flow proceeds to submit — this test targets the POST-SUBMIT path.
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())
    seen = {}

    # Right after clicking Login the XHR POST has fired but the document
    # is STILL /nlogin/login and the auth avatar is NOT present yet.
    def after_click(_selector: str) -> None:
        seen["url_immediately_after_click"] = page.url
        assert login_module.is_authenticated(page) is False

    page.on_click = after_click

    # The JS redirect only "lands" when the code actually waits for the
    # authenticated-nav indicator — i.e. inside _settle_after_login_submit.
    def redirect_lands(_ready_selector: str) -> None:
        page.url = _HOME_URL
        page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_wait_for_selector = redirect_lands

    result = login_module.login(page, _settings())

    assert seen["url_immediately_after_click"] == _LOGIN_URL  # was genuinely stale
    assert result.status == LoginStatus.SUCCESS
    assert result.current_url == _HOME_URL
    # the settle waited on the DOM auth indicator, not just load-state
    assert any(
        selectors.AUTHENTICATED_NAV_INDICATOR in call[0]
        for call in page.wait_for_selector_calls
    )
    # success was decided by is_authenticated() (DOM), preferred over URL
    assert result.message == "Logged in."


def test_post_submit_settle_prefers_dom_indicator_over_transient_url():
    page = FakePage()
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())  # real login form -> post-submit path

    def redirect_lands(_sel: str) -> None:
        # DOM indicator appears but URL stays weird/transient
        page.url = "https://www.naukri.com/some/spa/interstitial"
        page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_wait_for_selector = redirect_lands

    result = login_module.login(page, _settings())
    assert result.status == LoginStatus.SUCCESS  # via is_authenticated(), not the URL fragment


def test_genuine_login_failure_still_raises_unexpected_page():
    """No auth indicator ever appears and the page stays on the login
    form (bad credentials) -> _settle times out, and login()'s
    classification still raises NaukriUnexpectedPageError."""
    page = FakePage()  # no on_wait_for_selector, no avatar, url stays on login form
    with pytest.raises(NaukriUnexpectedPageError):
        login_module.login(page, _settings())


def test_post_submit_captcha_is_not_delayed_by_the_new_settle_wait():
    """A genuine post-submit CAPTCHA still raises — the combined
    auth/challenge wait returns as soon as the challenge appears."""
    page = FakePage()

    def reveal_captcha(_sel: str) -> None:
        page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())

    page.on_wait_for_selector = reveal_captcha
    with pytest.raises(NaukriCaptchaError):
        login_module.login(page, _settings())


def test_post_submit_settle_never_raises_out_even_if_wait_for_selector_errors():
    page = FakePage()
    page.wait_for_selector_error = RuntimeError("selector engine boom")
    page.on_click = lambda _s: page.set_element(
        selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement()
    )
    # avatar present from the click; settle's wait_for_selector raising
    # must be swallowed, then is_authenticated() succeeds
    result = login_module.login(page, _settings())
    assert result.status == LoginStatus.SUCCESS


# --- diagnostic-only login-step logging (repeated discovery TimeoutError) ---


def test_login_step_diagnostic_names_goto_as_the_failing_operation(caplog):
    """A timeout in page.goto(LOGIN_URL) must be re-raised unchanged AND
    logged with the operation name + the real Playwright message."""
    page = FakePage()
    boom = TimeoutError('Page.goto: Timeout 30000ms exceeded.\nnavigating to "%s"' % selectors.LOGIN_URL)

    def raising_goto(url: str) -> None:
        page.goto_calls.append(url)
        raise boom

    page.goto = raising_goto

    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        with pytest.raises(TimeoutError):
            login_module.login(page, _settings())

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "page.goto(LOGIN_URL) FAILED" in msg
    assert "TimeoutError" in msg
    assert "Timeout 30000ms exceeded" in msg  # the actual Playwright text
    # the attempt was announced before it ran
    assert any(
        "attempting page.goto(LOGIN_URL)" in r.getMessage() for r in caplog.records
    )
    # it never got as far as the form
    assert page.filled == {} and page.clicked == []


def test_login_step_diagnostic_names_email_fill_as_the_failing_operation(tmp_path, caplog):
    page = FakePage()
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())  # a real login form is present
    page.fill_error = TimeoutError("Page.fill: Timeout 30000ms exceeded.")

    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        with pytest.raises(TimeoutError):
            login_module.login(page, _settings(inspection_output_dir=tmp_path))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # only the step failure; the login form is a recognised state
    msg = errors[0].getMessage()
    assert "page.fill(LOGIN_EMAIL_INPUT) FAILED" in msg
    assert "TimeoutError" in msg
    assert page.goto_calls == [selectors.LOGIN_URL]  # goto succeeded, fill did not


def test_login_step_diagnostic_is_silent_on_the_happy_path(tmp_path, caplog):
    page = FakePage()
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())  # a real login form is present
    page.on_click = lambda _s: setattr(page, "url", "https://www.naukri.com/mnjuser/homepage")
    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        result = login_module.login(page, _settings(inspection_output_dir=tmp_path))
    assert result.status == LoginStatus.SUCCESS
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"


# --- run-4 fix: recognise the authenticated /mnjuser homepage after a ---
# --- delayed client redirect + nav hydration, without touching the form ---


def test_authenticated_homepage_recognized_after_delayed_nav_hydration():
    """goto(LOGIN_URL) redirects to /mnjuser/homepage but the nav avatar
    hydrates a beat later. The bounded post-goto settle must still
    recognise the session and never fill the login form (the run-4 bug:
    is_authenticated() queried too early, returned False, and login()
    then blocked filling a form that wasn't on the page)."""
    page = FakePage()

    def hydrate_home_on_wait(_ready: str) -> None:
        page.url = _HOME_URL
        page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_wait_for_selector = hydrate_home_on_wait

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert "redirected" in result.message.lower()
    assert result.current_url == _HOME_URL
    assert page.goto_calls == [selectors.LOGIN_URL]
    assert page.filled == {} and page.clicked == []


def test_authenticated_recognized_by_mnjuser_redirect_url_when_avatar_not_seen():
    """Second, independent signal: even if the avatar never attaches
    within the settle window, 'the login page redirected us to a
    naukri.com/mnjuser URL' is proof of an authenticated session."""
    page = FakePage()

    def land_home_url_only(_ready: str) -> None:
        page.url = _HOME_URL  # redirected; avatar never appears in-window

    page.on_wait_for_selector = land_home_url_only

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert result.current_url == _HOME_URL
    assert page.filled == {} and page.clicked == []


def test_genuinely_logged_out_login_page_runs_normal_form_flow():
    """A real logged-out user: /nlogin/login serves the email field and
    does not redirect. The post-goto settle resolves immediately on that
    field (no wasted wait), and the normal fill/submit flow runs."""
    page = FakePage()
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())  # the login form is present
    misses: list = []
    page.on_wait_for_selector = lambda sel: misses.append(sel)  # fires only on a wait MISS

    def redirect_after_submit(_sel: str) -> None:
        page.url = _HOME_URL
        page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    page.on_click = redirect_after_submit

    result = login_module.login(page, _settings())

    assert result.status == LoginStatus.SUCCESS
    assert result.message == "Logged in."  # normal form flow, not the "redirected" shortcut
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"
    assert page.filled[selectors.LOGIN_PASSWORD_INPUT] == "s3cret"
    assert page.clicked == [selectors.LOGIN_SUBMIT_BUTTON]
    assert misses == []  # post-goto settle hit the visible login field; no budget burned


# --- run-5 diagnostic: capture page state at the post-goto point ---


def test_capture_login_state_logs_summary_and_writes_no_artifact_for_a_login_form(
    tmp_path, caplog
):
    page = FakePage()
    page.url = selectors.LOGIN_URL
    page.page_title = "Naukri Login"
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())

    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        login_module._capture_login_state(
            page, _settings(inspection_output_dir=tmp_path), "post_goto"
        )

    line = next(r.getMessage() for r in caplog.records if "login-state[post_goto]" in r.getMessage())
    assert repr(selectors.LOGIN_URL) in line
    assert "'Naukri Login'" in line
    assert "#usernameField=True" in line
    assert "avatar=False" in line
    # recognised state -> no snapshot dir created
    assert not (tmp_path / "login_diagnostics").exists()


def test_capture_login_state_writes_html_and_png_for_an_unrecognised_page(tmp_path, caplog):
    page = FakePage()
    page.url = "https://www.naukri.com/"
    page.page_title = "Access Denied"
    page.content_html = "<html><body>Please verify you are a human. Reference #12ab</body></html>"
    # no #usernameField, no avatar -> unrecognised

    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        login_module._capture_login_state(
            page, _settings(inspection_output_dir=tmp_path), "post_goto"
        )

    diag = tmp_path / "login_diagnostics"
    htmls = list(diag.glob("*_post_goto.html"))
    pngs = list(diag.glob("*_post_goto.png"))
    assert len(htmls) == 1 and len(pngs) == 1
    assert "verify you are a human" in htmls[0].read_text(encoding="utf-8")
    summary = next(r.getMessage() for r in caplog.records if "login-state[post_goto]" in r.getMessage())
    assert "markers=[" in summary and "verify you are" in summary
    assert any(
        r.levelno == logging.ERROR and "snapshot written to" in r.getMessage()
        for r in caplog.records
    )


def test_capture_login_state_never_raises_even_if_every_page_method_throws(tmp_path):
    page = FakePage()

    def boom(*_a, **_k):
        raise RuntimeError("driver gone")

    page.content = boom
    page.title = boom
    page.screenshot = boom
    page.query_selector = boom
    page.query_selector_all = boom
    # must simply return, no exception
    login_module._capture_login_state(
        page, _settings(inspection_output_dir=tmp_path), "post_goto"
    )


def test_capture_login_state_does_not_change_login_outcome(tmp_path):
    page = FakePage()
    page.url = selectors.LOGIN_URL
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())
    page.on_click = lambda _s: setattr(page, "url", _HOME_URL)

    result = login_module.login(page, _settings(inspection_output_dir=tmp_path))

    assert result.status == LoginStatus.SUCCESS
    assert page.filled[selectors.LOGIN_EMAIL_INPUT] == "candidate@example.com"
    assert not (tmp_path / "login_diagnostics").exists()  # login form is a recognised state


def test_post_goto_state_capture_and_auth_check_are_logged_in_the_flow(tmp_path, caplog):
    """The diagnostic call site is wired into login() right after goto,
    and _authenticated_after_goto now logs its decision at INFO."""
    page = FakePage()
    page.url = selectors.LOGIN_URL
    page.set_element(selectors.LOGIN_EMAIL_INPUT, FakeElement())
    page.on_click = lambda _s: setattr(page, "url", _HOME_URL)

    with caplog.at_level(logging.INFO, logger="naukri_agent.browser.login"):
        login_module.login(page, _settings(inspection_output_dir=tmp_path))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("login-state[post_goto]" in m for m in msgs)
    assert any(m.startswith("post-goto auth check: avatar=False") for m in msgs)
