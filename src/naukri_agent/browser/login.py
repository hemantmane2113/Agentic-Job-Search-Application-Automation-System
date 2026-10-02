"""
Login: fills Naukri's login form and classifies the resulting page
state. Never attempts to solve a CAPTCHA or MFA challenge — those are
raised as exceptions for a human to handle (the interactive `inspect`
CLI command pauses for manual intervention when it sees one).
"""

from __future__ import annotations

import datetime
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

# Bounded, non-fatal settle budget for the navigation that follows the
# login submit. Used with 'domcontentloaded' / 'load' — NEVER
# 'networkidle' (see _settle_after_login_submit).
_POST_SUBMIT_SETTLE_TIMEOUT_MS = 15000


def _login_step(description: str, action, *, url: str | None = None):
    """
    Diagnostic-only wrapper around a single raw Playwright login call.

    Logs (INFO) the operation about to run, and on ANY exception logs
    (ERROR) the operation name plus the real Playwright message — which
    already embeds the URL and call log — then re-raises the exception
    unchanged. It adds NO handling, retry, or timeout: behaviour is
    byte-for-byte identical to calling `action()` directly. Purpose: a
    repeated "discovery failed: TimeoutError" gives no traceback in the
    logs, so this pins down WHICH of goto / email fill / password fill /
    submit click is the one timing out.
    """
    logger.info("login: attempting %s%s", description, f" (url={url})" if url else "")
    try:
        return action()
    except Exception as exc:  # noqa: BLE001 - log-and-re-raise only; never swallows
        logger.error(
            "login: %s FAILED — %s: %s",
            description,
            type(exc).__name__,
            exc,
        )
        raise


_LOGIN_STATE_MARKERS = (
    "access denied",
    "unusual",
    "verify you are",
    "are a human",
    "not a robot",
    "too many request",
    "captcha",
    "something went wrong",
    "temporarily blocked",
    "enable javascript",
    "reference #",  # Akamai / edge error pages
)


def _capture_login_state(page: Any, settings: Settings, label: str) -> None:
    """
    DIAGNOSTIC ONLY. Best-effort snapshot of the page state at a point in
    the login flow, so a login failure can be explained after the fact —
    what URL / title / DOM / selectors were actually present.

    Never raises, and never changes control flow, timeout values,
    selectors, or authentication logic: login()'s outcome is identical
    whether this runs, succeeds, or fails. Always logs a one-line
    summary; additionally writes a full HTML + PNG snapshot ONLY when the
    page is neither a recognisable login form (#usernameField present)
    nor a recognisable authenticated page (avatar present) — i.e. exactly
    the states we currently cannot explain.
    """
    try:
        url = _safe_current_url(page)
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = None

        has_email = _is_present(page, selectors.LOGIN_EMAIL_INPUT)
        has_pw = _is_present(page, selectors.LOGIN_PASSWORD_INPUT)
        has_submit = _is_present(page, selectors.LOGIN_SUBMIT_BUTTON)
        has_avatar = _is_present(page, selectors.AUTHENTICATED_NAV_INDICATOR)
        has_captcha_ind = _is_present(page, selectors.CAPTCHA_INDICATOR)
        has_grecaptcha = _is_present(page, selectors.RECAPTCHA_STATIC_CONTAINER)
        has_mfa = _is_present(page, selectors.MFA_INDICATOR)
        n_iframe = len(_query_all(page, "iframe"))
        n_form = len(_query_all(page, "form"))
        n_input = len(_query_all(page, "input"))

        try:
            html = page.content() or ""
        except Exception:  # noqa: BLE001
            html = ""
        markers = [m for m in _LOGIN_STATE_MARKERS if m in html.lower()]

        logger.info(
            "login-state[%s]: url=%r title=%r | #usernameField=%s #passwordField=%s "
            "submit=%s avatar=%s captchaIndicator=%s g-recaptcha=%s mfa=%s | "
            "iframes=%d forms=%d inputs=%d htmlBytes=%d markers=%s",
            label, url, title, has_email, has_pw, has_submit, has_avatar,
            has_captcha_ind, has_grecaptcha, has_mfa, n_iframe, n_form, n_input,
            len(html), markers or "-",
        )

        if has_email or has_avatar:
            return  # a clean, recognised state — no artifact needed

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = settings.inspection_output_dir / "login_diagnostics"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            if html:
                (out_dir / f"{stamp}_{label}.html").write_text(html, encoding="utf-8")
            try:
                page.screenshot(
                    path=str(out_dir / f"{stamp}_{label}.png"), full_page=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("login-state[%s]: screenshot failed: %s", label, exc)
            logger.error(
                "login-state[%s]: page is neither a login form nor an authenticated "
                "page — snapshot written to %s (%s_%s.*)",
                label, out_dir, stamp, label,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("login-state[%s]: could not write snapshot: %s", label, exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break login
        logger.debug("login-state[%s]: capture failed entirely: %s", label, exc)


def _settle_after_login_submit(page: Any) -> None:
    """
    Wait — bounded, no fixed sleeps — for the state AFTER clicking Login
    to actually resolve, then hand back to login()'s CAPTCHA / MFA /
    is_authenticated() / URL-fragment classification.

    Naukri's login submit is an XHR POST — it does NOT navigate the
    current /nlogin/login document — followed by a JS redirect to an
    authenticated page. `wait_for_load_state()` only observes the
    CURRENT (already fully-loaded) login document, so it returns
    instantly and does NOT wait for that redirect. login() would then
    read a STALE `page.url` (still /nlogin/login) and a not-yet-present
    auth indicator, and wrongly raise NaukriUnexpectedPageError — the
    exact failure of a live run whose network log shows the login POST
    landing on /mnjuser/homepage a moment later.

    Also NOT wait_for_load_state("networkidle"): the authenticated SPA
    keeps background connections open, so it never goes idle and the
    wait burns its full timeout.

    So wait for the FIRST of:
      * the VERIFIED authenticated-nav indicator (the redirect landed —
        the preferred, DOM-based signal), or
      * a CAPTCHA / MFA challenge element,
    whichever appears. `wait_for_selector` re-evaluates across the
    navigation, so this spans the redirect without any sleep. A genuine
    credential failure (page stays on the login form, no indicator)
    simply times out here and falls through to login()'s classification,
    which still raises NaukriUnexpectedPageError. The wait is bounded and
    every exception is swallowed.
    """
    ready_selector = ", ".join(
        (
            selectors.AUTHENTICATED_NAV_INDICATOR,
            selectors.CAPTCHA_INDICATOR,
            selectors.MFA_INDICATOR,
        )
    )
    try:
        page.wait_for_selector(
            ready_selector, state="attached", timeout=_POST_SUBMIT_SETTLE_TIMEOUT_MS
        )
    except Exception as exc:  # noqa: BLE001 - bounded settle; timing out here is not fatal
        logger.debug(
            "post-submit: no auth/challenge indicator within %dms (%s); "
            "proceeding to classification",
            _POST_SUBMIT_SETTLE_TIMEOUT_MS,
            exc,
        )

    # Best-effort: let whichever document we ended on finish loading, so
    # the checks below run against a stable DOM rather than mid-transition.
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=_POST_SUBMIT_SETTLE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - bounded settle, never fatal
            logger.debug(
                "post-submit wait_for_load_state(%r) raised %s; continuing to auth checks",
                state,
                exc,
            )


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


def _authenticated_after_goto(page: Any) -> bool:
    """
    Decide, right after goto(LOGIN_URL), whether this persistent session
    is already logged in.

    An authenticated session is client-side-redirected off /nlogin/login
    to /mnjuser/*, and BOTH that redirect and the global-nav avatar it
    renders can lag the load event. A single instantaneous
    is_authenticated() query therefore runs too early and misses them —
    observed live: goto reached /mnjuser/homepage, the avatar was not yet
    in the DOM, and login() then blocked for the full default timeout
    filling a login form that wasn't on the page.

    So first wait — bounded, non-fatal, no fixed sleep — for the FIRST
    of the authenticated-nav avatar or the login email field to attach
    (a genuinely logged-out page shows the field immediately, so the
    wait ends at once and login proceeds unchanged). Then treat the
    session as authenticated on EITHER independent signal:
      * the VERIFIED authenticated-nav avatar is present, or
      * navigating to the login page redirected us onto a
        naukri.com/mnjuser URL — a logged-out user stays on /nlogin/login.
    """
    ready = ", ".join(
        (selectors.AUTHENTICATED_NAV_INDICATOR, selectors.LOGIN_EMAIL_INPUT)
    )
    try:
        page.wait_for_selector(
            ready, state="attached", timeout=_POST_SUBMIT_SETTLE_TIMEOUT_MS
        )
    except Exception as exc:  # noqa: BLE001 - bounded settle; absence here is not fatal
        logger.info(  # promoted from debug: this is the run-5 failure precondition
            "post-goto settle: neither auth avatar nor #usernameField attached "
            "within %dms (%s); proceeding to classification",
            _POST_SUBMIT_SETTLE_TIMEOUT_MS,
            exc,
        )
    avatar = is_authenticated(page)
    url = _safe_current_url(page) or ""
    redirected = selectors.LOGIN_SUCCESS_URL_FRAGMENT in url
    logger.info(
        "post-goto auth check: avatar=%s url=%r mnjuser_redirect=%s",
        avatar,
        url,
        redirected,
    )
    return avatar or redirected


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
        logger.info(
            "login: persistent session already authenticated — login form not touched"
        )
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already authenticated (persistent session) — login form never touched.",
            current_url=getattr(page, "url", None),
        )

    logger.info("login: no existing session detected — proceeding to the login form")
    _login_step(
        "page.goto(LOGIN_URL)",
        lambda: page.goto(selectors.LOGIN_URL),
        url=selectors.LOGIN_URL,
    )

    # DIAGNOSTIC ONLY: record what page.goto(LOGIN_URL) actually produced
    # (url / title / DOM / selectors) — no effect on the flow below.
    _capture_login_state(page, settings, "post_goto")

    # goto(LOGIN_URL) may itself have redirected straight to an
    # authenticated page if the persistent session was valid — check
    # again (with a bounded settle for the redirect + nav hydration)
    # before assuming we're actually looking at a login form.
    if _authenticated_after_goto(page):
        logger.info(
            "login: authenticated after goto redirect — login form not touched"
        )
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already authenticated after navigating to the login page (redirected) — login form never touched.",
            current_url=_safe_current_url(page),
        )

    if _captcha_challenge_present(page):
        raise NaukriCaptchaError(
            "CAPTCHA presented before the login form could be submitted. "
            "This must be completed manually — automation will not solve "
            "or circumvent it. Whether execution pauses for you to do so "
            "depends on the caller (naukri-agent inspect does)."
        )

    _login_step(
        "page.fill(LOGIN_EMAIL_INPUT)",
        lambda: page.fill(selectors.LOGIN_EMAIL_INPUT, settings.naukri_email),
    )
    _login_step(
        "page.fill(LOGIN_PASSWORD_INPUT)",
        lambda: page.fill(selectors.LOGIN_PASSWORD_INPUT, settings.naukri_password),
    )
    _login_step(
        "page.click(LOGIN_SUBMIT_BUTTON)",
        lambda: page.click(selectors.LOGIN_SUBMIT_BUTTON),
    )
    logger.info("login: form submitted — settling and classifying resulting page state")
    _settle_after_login_submit(page)

    if _captcha_challenge_present(page):
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


def _captcha_challenge_present(page: Any) -> bool:
    """
    True ONLY for an active, human-solvable CAPTCHA challenge.

    Naukri's normal login page always contains a static, non-interactive
    `<div class="g-recaptcha" ... data-size="invisible">` container even
    when no challenge is shown (confirmed against real login-page
    captures — see selectors.py). Treating that as a challenge produced
    a false-positive NaukriCaptchaError on every fresh-profile login.

    A genuine challenge is either:
      * something matching selectors.CAPTCHA_INDICATOR — a challenge
        iframe (generic captcha / reCAPTCHA api2/bframe), an explicit
        #captcha, or a `.g-recaptcha` that is NOT data-size="invisible";
      * a `.g-recaptcha` container whose `data-size` attribute is
        anything other than "invisible" (checked directly here as well,
        in case the CSS :not() form isn't honoured by the engine or the
        attribute value is cased oddly).

    This only DETECTS a challenge so a human can handle it. It never
    interacts with the challenge. The human-in-the-loop pause behaviour
    is unchanged.
    """
    if _is_present(page, selectors.CAPTCHA_INDICATOR):
        return True
    for el in _query_all(page, selectors.RECAPTCHA_STATIC_CONTAINER):
        if (_attr(el, "data-size") or "").strip().lower() != "invisible":
            return True
    return False


def _query_all(page: Any, selector: str) -> list:
    try:
        return page.query_selector_all(selector) or []
    except Exception as exc:  # noqa: BLE001 - a selector-engine error means "nothing", not a crash
        logger.debug("query_selector_all(%r) raised %s; treating as empty", selector, exc)
        return []


def _attr(el: Any, name: str) -> str | None:
    try:
        return el.get_attribute(name)
    except Exception:  # noqa: BLE001
        return None


def _is_present(page: Any, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception as exc:  # noqa: BLE001 - a selector-engine error means "not present", not a crash
        logger.debug("query_selector(%r) raised %s; treating as not present", selector, exc)
        return False


def _safe_current_url(page: Any) -> str | None:
    """
    Read page.url defensively. When the Playwright connection is
    already dead (e.g. the user closed the browser window during a
    manual CAPTCHA), the `url` property RAISES rather than being
    absent, so `getattr(page, "url", default)` is not enough. Returns
    None if the URL cannot be read.
    """
    try:
        return page.url
    except Exception as exc:  # noqa: BLE001
        logger.debug("page.url unreadable (%s); treating URL signal as unavailable", exc)
        return None


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
            current_url=_safe_current_url(page),
        )

    current_url = _safe_current_url(page) or ""
    if selectors.LOGIN_SUCCESS_URL_FRAGMENT in current_url:
        return LoginResult(
            status=LoginStatus.SUCCESS,
            message="Already logged in (detected via URL; DOM indicator not found).",
            current_url=current_url,
        )
    return None
