"""
Tests for browser.inspection.run_inspection's CAPTCHA/MFA handling.

BrowserManager itself launches a real browser, which this sandbox
can't do -- so these tests monkeypatch inspection.BrowserManager with
a FakeBrowserManager wrapping a FakePage, giving full control over
page state (including simulating what a human's manual completion of
a challenge would change) without any real browser or network access.
"""

from __future__ import annotations

from naukri_agent.browser import selectors
from naukri_agent.browser.inspection import run_inspection
from naukri_agent.config import Settings

from .browser_fakes import FakeElement, FakePage


class FakeBrowserManager:
    """Stands in for browser.browser_manager.BrowserManager in tests."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.page = FakePage()
        self.closed = False

    def __enter__(self) -> "FakeBrowserManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.closed = True


def _settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        naukri_email="a@b.com",
        naukri_password="pw",
        inspection_output_dir=tmp_path / "inspection_output",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _patch_browser_manager(monkeypatch, manager: FakeBrowserManager) -> None:
    import naukri_agent.browser.inspection as inspection_module

    monkeypatch.setattr(inspection_module, "BrowserManager", lambda settings: manager)


def _clear_login_page_and_succeed(page: FakePage) -> None:
    """Simulate the page navigating to a normal, successful post-login state."""
    page.url = "https://www.naukri.com/mnjuser/homepage"
    # search_jobs / get_job need at least an empty listing to not error
    page.set_element(selectors.JOB_CARD, [])


# --- 1 & 2: CAPTCHA detected -> browser stays open while waiting -> resumes ---


def test_captcha_detected_browser_remains_open_while_waiting(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    observed_closed_state = {}

    def fake_wait(prompt: str) -> None:
        # At the moment we're paused, the browser must NOT have been closed.
        observed_closed_state["closed_during_pause"] = manager.closed
        # Simulate the human completing the CAPTCHA and being redirected.
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        _clear_login_page_and_succeed(manager.page)

    run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert observed_closed_state["closed_during_pause"] is False


def test_user_completes_captcha_inspection_resumes_and_completes(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        _clear_login_page_and_succeed(manager.page)

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert report["completed"] is True
    step_names = [s["step"] for s in report["steps"]]
    assert "login" in step_names
    assert manager.closed is True  # closed normally at the end, not during the pause


# --- 3: challenge already cleared -> no duplicate login ---


def test_challenge_already_cleared_does_not_repeat_login(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        # Human already completed everything and landed on the success page
        # BEFORE we even ask them to press Enter -- simulate that fully.
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        _clear_login_page_and_succeed(manager.page)

    run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    # login() would call goto(LOGIN_URL) and fill credentials; since
    # check_already_logged_in short-circuited, login() must never run
    # a second time -- so LOGIN_URL should appear in goto_calls only
    # from the FIRST (failed) attempt, and fields must never be filled.
    assert manager.page.filled == {}
    assert manager.page.goto_calls.count(selectors.LOGIN_URL) == 1


# --- 4 & 5: challenge still present after retry -> safe failure + cleanup ---


def test_challenge_still_present_after_retry_fails_safely_and_closes(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        pass  # human did NOT actually resolve it -- element stays present

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert report["completed"] is False
    assert report["stopped_at"] == "login"
    assert report["error_type"] == "NaukriCaptchaError"
    assert manager.closed is True  # browser IS closed after the safe failure


def test_retry_happens_exactly_once_not_looped(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    wait_call_count = {"n": 0}

    def fake_wait(prompt: str) -> None:
        wait_call_count["n"] += 1

    run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert wait_call_count["n"] == 1  # paused once, retried once, then gave up


# --- MFA: same behavior as CAPTCHA ---


def test_mfa_detected_pauses_and_resumes_on_completion(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.MFA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        assert "MFA" in prompt or "OTP" in prompt
        manager.page._elements.pop(selectors.MFA_INDICATOR, None)
        _clear_login_page_and_succeed(manager.page)

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)
    assert report["completed"] is True


def test_mfa_still_present_after_retry_fails_safely_and_closes(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.MFA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        pass  # not resolved

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)
    assert report["completed"] is False
    assert report["error_type"] == "NaukriMfaError"
    assert manager.closed is True


# --- 6: generic automation errors still take the existing fatal path ---


def test_generic_unexpected_page_error_never_pauses(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    # No CAPTCHA/MFA element, and page.url never matches the success
    # fragment after "submitting" -> login() raises NaukriUnexpectedPageError.
    _patch_browser_manager(monkeypatch, manager)

    wait_called = {"called": False}

    def fake_wait(prompt: str) -> None:
        wait_called["called"] = True

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert wait_called["called"] is False  # never paused for a non-challenge error
    assert report["completed"] is False
    assert report["error_type"] == "NaukriUnexpectedPageError"
    assert manager.closed is True


def test_missing_credentials_never_pauses_either(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path, naukri_email="", naukri_password=""))
    _patch_browser_manager(monkeypatch, manager)

    wait_called = {"called": False}
    report = run_inspection(
        _settings(tmp_path, naukri_email="", naukri_password=""),
        wait_for_manual_completion=lambda p: wait_called.__setitem__("called", True),
    )
    assert wait_called["called"] is False
    assert report["error_type"] == "NaukriLoginError"


# --- Authenticated-session guard, exercised through the full run_inspection flow ---


def test_captcha_then_settle_then_authenticated_via_dom_indicator_continues(tmp_path, monkeypatch):
    """
    Item 4: after the manual-completion pause, the page settles and
    the DOM auth indicator (not any URL) is what tells run_inspection
    to continue without repeating login().
    """
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    settle_called = {"n": 0}
    real_wait_for_load_state = manager.page.wait_for_load_state

    def counting_wait_for_load_state(state="load"):
        settle_called["n"] += 1
        return real_wait_for_load_state(state)

    manager.page.wait_for_load_state = counting_wait_for_load_state

    def fake_wait(prompt: str) -> None:
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        # Deliberately do NOT set a matching URL -- only the DOM
        # indicator, to prove that's what's actually being used.
        manager.page.url = "https://www.naukri.com/some/unrelated/path"
        manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())
        manager.page.set_element(selectors.JOB_CARD, [])

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert settle_called["n"] >= 1  # the page was given a chance to settle
    assert report["completed"] is True
    assert manager.page.filled == {}  # login() was never repeated


def test_captcha_then_settle_then_still_unauthenticated_retries_exactly_once(tmp_path, monkeypatch):
    """Item 5: settling doesn't fix the auth state -> exactly one retry actually runs login()."""
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        # Captcha resolved, but session still isn't authenticated by
        # any signal -- e.g. the human closed the dialog without
        # actually completing it.
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)

        def succeed_on_retry(url):
            manager.page.url = "https://www.naukri.com/mnjuser/homepage"
            manager.page.set_element(selectors.JOB_CARD, [])

        manager.page.on_goto = succeed_on_retry

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    # The retry actually called login(), which filled credentials.
    assert manager.page.filled[selectors.LOGIN_EMAIL_INPUT] == "a@b.com"
    assert report["completed"] is True


# --- Item 7: Playwright interaction failures produce a structured report ---


def test_playwright_timeout_during_login_produces_structured_report(tmp_path, monkeypatch):
    """
    The exact bug from the real run: Page.fill times out waiting for
    a locator that isn't on the page. This must produce a structured
    JSON report (with diagnostics) via the new narrow boundary, not
    an unhandled traceback.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.fill_error = PlaywrightTimeoutError(
        'Page.fill: Timeout 30000ms exceeded.\nwaiting for locator("#usernameField")'
    )
    _patch_browser_manager(monkeypatch, manager)

    report = run_inspection(_settings(tmp_path))  # no CAPTCHA involved; fails on the fill itself

    assert report["completed"] is False
    assert report["error_type"] == "TimeoutError"
    assert "Timeout 30000ms" in report["error"]
    assert report["stopped_at"] == "login"
    # goto(LOGIN_URL) runs before the fill that fails, so this is the
    # accurate page state at the moment of failure.
    assert report["current_url"] == selectors.LOGIN_URL
    assert manager.closed is True  # browser still cleaned up normally


def test_playwright_error_during_retry_also_produces_structured_report(tmp_path, monkeypatch):
    """The same boundary must catch a Playwright error raised during the CAPTCHA retry path too."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        # Still not authenticated after settling -> retry runs -> but
        # the retry's own fill() now times out.
        manager.page.fill_error = PlaywrightTimeoutError("Page.fill: Timeout 30000ms exceeded.")

    report = run_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)

    assert report["completed"] is False
    assert report["error_type"] == "TimeoutError"
    assert manager.closed is True


def test_unexpected_programming_error_is_not_swallowed(tmp_path, monkeypatch):
    """
    A genuine bug (not a Playwright interaction failure, not one of
    our NaukriAutomationError types) must NOT be caught by either
    boundary -- it should propagate normally, exactly as before.
    Injected via search_jobs's unwrapped query_selector_all() call.
    """
    import pytest

    manager = FakeBrowserManager(_settings(tmp_path))
    # Already authenticated -> login() short-circuits immediately, no
    # CAPTCHA/MFA path involved -- isolates the failure to the bug itself.
    manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    def broken_query_selector_all(selector: str):
        raise TypeError("something genuinely wrong -- not a Playwright/automation error")

    manager.page.query_selector_all = broken_query_selector_all
    _patch_browser_manager(monkeypatch, manager)

    with pytest.raises(TypeError):
        run_inspection(_settings(tmp_path))
