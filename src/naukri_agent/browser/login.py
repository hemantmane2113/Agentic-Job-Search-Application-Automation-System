"""
Login: fills Naukri's login form and classifies the resulting page
state. Never attempts to solve a CAPTCHA or MFA challenge — those are
raised as exceptions for a human to handle (the interactive `inspect`
CLI command pauses for manual intervention when it sees one).
"""

from __future__ import annotations

import logging
from typing import Any

from naukri_agent.browser import selectors
from naukri_agent.browser.exceptions import (
    NaukriCaptchaError,
    NaukriLoginError,
    NaukriMfaError,
    NaukriUnexpectedPageError,
)
from naukri_agent.browser.models import LoginResult, LoginStatus
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


def is_authenticated(page: Any) -> bool:
    """
    Read-only check for whether the current page reflects an
    authenticated Naukri session. Uses the profile-avatar element in
    the persistent global navigation header (selectors.
    AUTHENTICATED_NAV_INDICATOR) rather than any page URL — confirmed
    present across every authenticated page captured during Stage 1
    inspection (post-login, profile, search results), so it holds
    regardless of which specific page the browser happens to be on.
    Never navigates, fills, or clicks.
    """
    return _is_present(page, selectors.AUTHENTICATED_NAV_INDICATOR)


def login(page: Any, settings: Settings) -> LoginResult:
    """
    Attempt to log in using settings.naukri_email/naukri_password —
    read from environment variables via Settings, never hardcoded.

    Because BrowserManager uses a PERSISTENT browser profile, a
    previous successful login may already leave this session
    authenticated before this function is even called — and
    navigating to LOGIN_URL on an authenticated session commonly
    redirects straight past the login form. This function checks for
    that (read-only, via is_authenticated()) both before AND right
    after the navigation to LOGIN_URL, and returns success without
    ever touching the credential fields if either check finds the
    session already authenticated. This is what prevents the failure
    mode where login() blindly tries to fill a login form that isn't
    there anymore.

    Raises NaukriCaptchaError / NaukriMfaError immediately if either
    challenge is detected, before or after submitting the form. These
    are not failures to retry around — they're the signal to stop.
    """
    if not settings.naukri_email or not settings.naukri_password:
        raise NaukriLoginError(
            "NAUKRI_EMAIL / NAUKRI_PASSWORD are not set. Credentials must "
            "come from environment variables, never source code."
        )

    if is_authenticated(page):
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already authenticated (persistent session) — login form never touched.",
            current_url=getattr(page, "url", None),
        )

    page.goto(selectors.LOGIN_URL)

    # goto(LOGIN_URL) may itself have redirected straight to an
    # authenticated page if the persistent session was valid — check
    # again before assuming we're actually looking at a login form.
    if is_authenticated(page):
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already authenticated after navigating to the login page (redirected) — login form never touched.",
            current_url=page.url,
        )

    if _is_present(page, selectors.CAPTCHA_INDICATOR):
        raise NaukriCaptchaError(
            "CAPTCHA presented before the login form could be submitted. "
            "This must be completed manually — automation will not solve "
            "or circumvent it. Whether execution pauses for you to do so "
            "depends on the caller (naukri-agent inspect does)."
        )

    page.fill(selectors.LOGIN_EMAIL_INPUT, settings.naukri_email)
    page.fill(selectors.LOGIN_PASSWORD_INPUT, settings.naukri_password)
    page.click(selectors.LOGIN_SUBMIT_BUTTON)
    page.wait_for_load_state("networkidle")

    if _is_present(page, selectors.CAPTCHA_INDICATOR):
        raise NaukriCaptchaError(
            "CAPTCHA presented after submitting login. This must be "
            "completed manually — automation will not solve or "
            "circumvent it. Whether execution pauses for you to do so "
            "depends on the caller (naukri-agent inspect does)."
        )
    if _is_present(page, selectors.MFA_INDICATOR):
        raise NaukriMfaError(
            "MFA/OTP challenge presented after submitting login. This "
            "must be completed manually — automation will not solve or "
            "circumvent it. Whether execution pauses for you to do so "
            "depends on the caller (naukri-agent inspect does)."
        )

    if is_authenticated(page):
        return LoginResult(status=LoginStatus.SUCCESS, message="Logged in.", current_url=page.url)

    current_url = page.url
    if selectors.LOGIN_SUCCESS_URL_FRAGMENT in current_url:
        return LoginResult(status=LoginStatus.SUCCESS, message="Logged in.", current_url=current_url)

    raise NaukriUnexpectedPageError(
        f"Login did not reach the expected post-login page. Current URL: "
        f"{current_url!r}. Naukri's login flow may differ from what "
        f"browser/selectors.py assumes — run `naukri-agent inspect` to "
        f"investigate and update the selectors."
    )


def _is_present(page: Any, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception as exc:  # noqa: BLE001 - a selector-engine error means "not present", not a crash
        logger.debug("query_selector(%r) raised %s; treating as not present", selector, exc)
        return False


def check_already_logged_in(page: Any) -> LoginResult | None:
    """
    Non-destructive check for whether the page already shows a
    successful login — e.g. after a human manually completed a
    CAPTCHA/MFA challenge that a previous login() call paused on.
    NEVER navigates, fills, or clicks anything.

    Uses is_authenticated() — the same verified DOM signal login()
    itself relies on — as the primary check, so a missing/unexpected
    URL is never treated as proof of failure on its own. The URL
    fragment is still checked as a secondary, best-effort signal for
    cases the DOM indicator doesn't cover.
    """
    if is_authenticated(page):
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already logged in (detected after manual challenge completion).",
            current_url=getattr(page, "url", None),
        )

    current_url = getattr(page, "url", "") or ""
    if selectors.LOGIN_SUCCESS_URL_FRAGMENT in current_url:
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already logged in (detected via URL; DOM indicator not found).",
            current_url=current_url,
        )
    return None
